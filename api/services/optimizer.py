from typing import List, Dict, Any


class InfeasibleRouteError(Exception):
    """Raised when destination cannot be reached within vehicle range."""
    pass


def optimize_fuel_stops(
    total_distance_miles: float,
    candidate_stops: List[Dict[str, Any]],
    max_range_miles: float = 500.0,
    fuel_economy_mpg: float = 10.0,
    tank_capacity_gallons: float = 50.0,
    initial_fuel_price_estimate: float = 3.50,
) -> Dict[str, Any]:
    """
    Optimizes refueling stops along a driving route to minimize cash fuel expenditure
    while strictly enforcing maximum vehicle range (500 miles) between refueling stops.

    Assumptions & Policy:
      1. Departure with full tank (50 gallons = 500 miles range).
      2. No cash is paid for fuel already in the tank at departure.
      3. At chosen intermediate stops, the driver purchases fuel to cover the subsequent leg.
      4. Every driving leg (Origin -> Stop 1, Stop 1 -> Stop 2, ..., Stop K -> Destination)
         is strictly <= max_range_miles (500 miles).
      5. Uses Shortest-Path Dynamic Programming over a Directed Acyclic Graph (DAG),
         guaranteeing the globally minimum cash expenditure and optimal stop selection.
    """
    warnings = []

    # If route is shorter than max range, direct travel is feasible without intermediate stops
    if total_distance_miles <= max_range_miles:
        direct_gallons = round(total_distance_miles / fuel_economy_mpg, 2)
        direct_cost = round(direct_gallons * initial_fuel_price_estimate, 2)
        return {
            'selected_stops': [],
            'is_feasible': True,
            'legs': [{
                'from_label': 'Origin',
                'to_label': 'Destination',
                'from_mile': 0.0,
                'to_mile': total_distance_miles,
                'distance_miles': total_distance_miles,
                'gallons': direct_gallons,
                'cost_usd': direct_cost,
                'fuel_price': initial_fuel_price_estimate,
            }],
            'warnings': ["Trip is within vehicle maximum range (500 miles). No intermediate refueling stop required."],
        }

    # Filter candidate stations that lie strictly between origin and destination
    valid_candidates = [
        s for s in candidate_stops
        if 0 < s['mile_marker'] < total_distance_miles
    ]

    if not valid_candidates:
        raise InfeasibleRouteError(
            f"Trip distance ({total_distance_miles:.1f} miles) exceeds vehicle maximum range ({max_range_miles:.0f} miles), "
            f"but zero fuel stations were found along the route corridor."
        )

    # Sort strictly by mile marker
    valid_candidates.sort(key=lambda s: s['mile_marker'])

    # Build DAG nodes: Node 0 (Origin), Nodes 1..M (candidate stations), Node M+1 (Destination)
    nodes = []
    nodes.append({
        'type': 'origin',
        'label': 'Origin',
        'station': None,
        'mile_marker': 0.0,
        'price': 0.0,  # Starting fuel is already in the tank (0 cash out-of-pocket)
    })

    for s in valid_candidates:
        station = s['station']
        nodes.append({
            'type': 'station',
            'label': station.name,
            'station': station,
            'mile_marker': s['mile_marker'],
            'corridor_distance': s['corridor_distance_miles'],
            'price': station.primary_price,
        })

    nodes.append({
        'type': 'destination',
        'label': 'Destination',
        'station': None,
        'mile_marker': total_distance_miles,
        'price': 0.0,
    })

    n = len(nodes)

    # DP over DAG: dp[i] = min cash cost to complete trip from node i to destination
    dp = [float('inf')] * n
    parent = [-1] * n
    dp[n - 1] = 0.0

    for i in range(n - 2, -1, -1):
        for j in range(i + 1, n):
            leg_distance = nodes[j]['mile_marker'] - nodes[i]['mile_marker']
            if leg_distance > max_range_miles:
                break
            if leg_distance <= 0:
                continue

            # Leg i -> j: fuel is purchased at station i (if i > 0)
            if i == 0:
                leg_cash_cost = 0.0  # Already fueled at start
            else:
                leg_cash_cost = (leg_distance / fuel_economy_mpg) * nodes[i]['price']

            total_cost_from_i = leg_cash_cost + dp[j]
            if total_cost_from_i < dp[i]:
                dp[i] = total_cost_from_i
                parent[i] = j

    if dp[0] == float('inf'):
        # Route is not feasible under the 500-mile range constraint
        max_gap = 0.0
        gap_start = 0.0
        gap_end = 0.0
        for i in range(n - 1):
            gap = nodes[i + 1]['mile_marker'] - nodes[i]['mile_marker']
            if gap > max_gap:
                max_gap = gap
                gap_start = nodes[i]['mile_marker']
                gap_end = nodes[i + 1]['mile_marker']

        raise InfeasibleRouteError(
            f"Cannot complete route: A gap of {max_gap:.1f} miles between mile {gap_start:.1f} and "
            f"mile {gap_end:.1f} exceeds the vehicle's maximum driving range of {max_range_miles:.0f} miles."
        )

    # Reconstruct chosen stops and driving legs
    legs = []
    selected_stops = []
    curr = 0

    while curr != n - 1:
        next_node = parent[curr]
        dist = nodes[next_node]['mile_marker'] - nodes[curr]['mile_marker']
        gallons = round(dist / fuel_economy_mpg, 2)

        # For the leg curr -> next_node:
        if curr == 0:
            price = initial_fuel_price_estimate
            cost = round(gallons * price, 2)
        else:
            price = nodes[curr]['price']
            cost = round(gallons * price, 2)

        legs.append({
            'from_label': nodes[curr]['label'],
            'to_label': nodes[next_node]['label'],
            'from_mile': nodes[curr]['mile_marker'],
            'to_mile': nodes[next_node]['mile_marker'],
            'distance_miles': round(dist, 2),
            'gallons': gallons,
            'price': price,
            'cost_usd': cost,
        })

        # If next_node is a station, it will serve as a refueling stop for the leg AFTER it
        if nodes[next_node]['type'] == 'station':
            after_next = parent[next_node]
            leg_after_dist = nodes[after_next]['mile_marker'] - nodes[next_node]['mile_marker']
            gallons_to_pump = round(leg_after_dist / fuel_economy_mpg, 2)
            st = nodes[next_node]['station']
            cost_at_station = round(gallons_to_pump * st.primary_price, 2)

            selected_stops.append({
                'station': st,
                'mile_marker': nodes[next_node]['mile_marker'],
                'corridor_distance_miles': nodes[next_node].get('corridor_distance', 0.0),
                'price_per_gallon': st.primary_price,
                'gallons_purchased': gallons_to_pump,
                'cost_usd': cost_at_station,
            })

        curr = next_node

    return {
        'selected_stops': selected_stops,
        'is_feasible': True,
        'legs': legs,
        'warnings': warnings,
    }
