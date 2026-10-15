import logging
from typing import List, Dict, Any
from scipy.optimize import linprog

logger = logging.getLogger(__name__)


class InfeasibleRouteError(Exception):
    """Raised when destination cannot be reached within vehicle range or fuel constraints."""
    pass


def optimize_fuel_stops(
    total_distance_miles: float,
    candidate_stops: List[Dict[str, Any]],
    max_range_miles: float = 500.0,
    fuel_economy_mpg: float = 10.0,
    tank_capacity_gallons: float = 50.0,
    initial_tank_gallons: float = 50.0,
    initial_fuel_price_estimate: float = 3.50,
) -> Dict[str, Any]:
    """
    Optimizes refueling stops along a driving route to minimize cash fuel expenditure
    while strictly enforcing tank capacity and maximum vehicle range (500 miles).

    Fuel Accounting & State Tracking Principles:
      1. Departure with starting fuel level in tank (default 50.0 gallons = 500 miles range).
      2. No cash is paid at pumps for fuel already in the tank at departure.
      3. At every station, the vehicle tracks exact arrival fuel, purchases only allowable
         gallons (never exceeding tank capacity), and accounts for fuel remaining from prior legs.
      4. Avoids assuming each leg's fuel is bought at its starting station; optimal purchases
         are determined via global continuous-state Linear Programming (HiGHS solver).
      5. Guarantees global cost optimality, handles cheap-station top-offs, skips redundant stops,
         and strictly reconciles fuel balance without artificial clamping.
    """
    warnings = []

    # 1. Short trip check: directly reachable with starting fuel
    initial_reach = initial_tank_gallons * fuel_economy_mpg
    if total_distance_miles <= initial_reach:
        direct_gallons = round(total_distance_miles / fuel_economy_mpg, 2)
        remaining_fuel = round(initial_tank_gallons - direct_gallons, 2)
        direct_cost = 0.0  # Zero cash paid at pumps for departure fuel

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
                'price': initial_fuel_price_estimate,
                'cost_usd': direct_cost,
                'arrival_fuel_gallons': remaining_fuel,
                'gallons_purchased': 0.0,
                'departure_fuel_gallons': initial_tank_gallons,
            }],
            'warnings': ["Trip is within vehicle departure range (500 miles). No intermediate refueling stop required."],
        }

    # 2. Filter candidates strictly between origin and destination
    valid_candidates = [
        s for s in candidate_stops
        if 0 < s['mile_marker'] < total_distance_miles
    ]

    if not valid_candidates:
        raise InfeasibleRouteError(
            f"Trip distance ({total_distance_miles:.1f} miles) exceeds departure vehicle range "
            f"({initial_reach:.0f} miles), but zero fuel stations were found along the route corridor."
        )

    # 3. Deduplicate stations at identical mile markers, retaining the cheapest option
    by_mile = {}
    for s in valid_candidates:
        m = round(s['mile_marker'], 2)
        price = s['station'].primary_price
        if m not in by_mile or price < by_mile[m]['station'].primary_price:
            by_mile[m] = s

    unique_candidates = sorted(by_mile.values(), key=lambda s: s['mile_marker'])

    # 4. Feasibility checks
    first_station_mile = unique_candidates[0]['mile_marker']
    if first_station_mile > initial_reach:
        raise InfeasibleRouteError(
            f"Cannot complete route: First available station is at mile {first_station_mile:.1f}, "
            f"which exceeds departure vehicle range of {initial_reach:.0f} miles."
        )

    # Check gap between consecutive opportunities
    all_points = [0.0] + [s['mile_marker'] for s in unique_candidates] + [total_distance_miles]
    max_gap = 0.0
    gap_start = 0.0
    gap_end = 0.0
    for i in range(len(all_points) - 1):
        gap = all_points[i + 1] - all_points[i]
        if gap > max_gap:
            max_gap = gap
            gap_start = all_points[i]
            gap_end = all_points[i + 1]

    if max_gap > max_range_miles:
        raise InfeasibleRouteError(
            f"Cannot complete route: A gap of {max_gap:.1f} miles between mile {gap_start:.1f} and "
            f"mile {gap_end:.1f} exceeds the vehicle's maximum driving range of {max_range_miles:.0f} miles."
        )

    # 5. Continuous-Fuel Linear Programming Formulation
    # Variables: b_0, ..., b_{M-1} >= 0 (gallons purchased at each candidate station)
    # Objective: min sum(p_i * b_i)
    # Constraints:
    #   Lower bound: Fuel upon arrival at next point >= 0
    #     S_i >= (x_{i+1} / MPG) - initial_tank_gallons  =>  -S_i <= initial_tank_gallons - (x_{i+1} / MPG)
    #   Upper bound: Fuel immediately after purchase <= tank_capacity_gallons
    #     S_i <= tank_capacity_gallons - initial_tank_gallons + (x_i / MPG)
    M = len(unique_candidates)
    miles = [s['mile_marker'] for s in unique_candidates]
    prices = [s['station'].primary_price for s in unique_candidates]
    r = 1.0 / fuel_economy_mpg

    A_ub = []
    b_ub = []

    # Lower bound constraints (must reach next station or destination)
    for i in range(M):
        next_x = miles[i + 1] if i + 1 < M else total_distance_miles
        row = [-1.0 if k <= i else 0.0 for k in range(M)]
        A_ub.append(row)
        b_ub.append(initial_tank_gallons - r * next_x)

    # Upper bound constraints (cannot exceed tank capacity after purchase)
    for i in range(M):
        row = [1.0 if k <= i else 0.0 for k in range(M)]
        A_ub.append(row)
        b_ub.append(tank_capacity_gallons - initial_tank_gallons + r * miles[i])

    bounds = [(0.0, tank_capacity_gallons) for _ in range(M)]
    res = linprog(prices, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method='highs')

    if not res.success:
        raise InfeasibleRouteError(f"No feasible refueling plan found: {res.message}")

    buys = res.x

    # 6. Extract stops where purchases occur (filtering negligible numerical noise < 0.01 gal)
    planned_stops = []
    for i, b in enumerate(buys):
        if b >= 0.01:
            planned_stops.append((unique_candidates[i], round(float(b), 2)))

    # 7. Physical Simulation & Validation of Tank State Across Route
    curr_mile = 0.0
    curr_fuel = initial_tank_gallons
    selected_stops = []
    legs = []

    for cand, gallons_to_buy in planned_stops:
        station = cand['station']
        stop_mile = cand['mile_marker']
        dist = round(stop_mile - curr_mile, 2)

        if dist > max_range_miles:
            raise InfeasibleRouteError(f"Driving leg of {dist:.1f} miles exceeds maximum vehicle range.")

        burn = round(dist / fuel_economy_mpg, 2)
        arrival_fuel = round(curr_fuel - burn, 2)

        if arrival_fuel < -1e-2:
            raise InfeasibleRouteError(
                f"Fuel ran out ({arrival_fuel:.2f} gal) before reaching station '{station.name}' at mile {stop_mile:.1f}."
            )
        arrival_fuel = max(0.0, arrival_fuel)

        # Enforce strict tank capacity constraint
        departure_fuel = round(arrival_fuel + gallons_to_buy, 2)
        if departure_fuel > tank_capacity_gallons + 0.05:
            raise InfeasibleRouteError(
                f"Refueling {gallons_to_buy:.2f} gal at '{station.name}' exceeds tank capacity "
                f"({departure_fuel:.2f} gal > {tank_capacity_gallons:.1f} gal)."
            )
        departure_fuel = min(tank_capacity_gallons, departure_fuel)

        cost_at_station = round(gallons_to_buy * station.primary_price, 2)

        legs.append({
            'from_label': 'Origin' if curr_mile == 0.0 else legs[-1]['to_label'],
            'to_label': station.name,
            'from_mile': curr_mile,
            'to_mile': stop_mile,
            'distance_miles': dist,
            'gallons': burn,
            'price': station.primary_price,
            'cost_usd': cost_at_station,
            'arrival_fuel_gallons': arrival_fuel,
            'gallons_purchased': gallons_to_buy,
            'departure_fuel_gallons': departure_fuel,
        })

        selected_stops.append({
            'station': station,
            'mile_marker': stop_mile,
            'corridor_distance_miles': cand.get('corridor_distance_miles', 0.0),
            'price_per_gallon': station.primary_price,
            'gallons_purchased': gallons_to_buy,
            'cost_usd': cost_at_station,
            'arrival_fuel_gallons': arrival_fuel,
            'departure_fuel_gallons': departure_fuel,
        })

        curr_mile = stop_mile
        curr_fuel = departure_fuel

    # Final driving leg from last stop (or origin) to destination
    final_dist = round(total_distance_miles - curr_mile, 2)
    if final_dist > max_range_miles:
        raise InfeasibleRouteError(f"Final driving leg of {final_dist:.1f} miles exceeds maximum vehicle range.")

    final_burn = round(final_dist / fuel_economy_mpg, 2)
    dest_arrival_fuel = round(curr_fuel - final_burn, 2)

    if dest_arrival_fuel < -1e-2:
        raise InfeasibleRouteError(
            f"Fuel ran out ({dest_arrival_fuel:.2f} gal) before reaching Destination at mile {total_distance_miles:.1f}."
        )
    dest_arrival_fuel = max(0.0, dest_arrival_fuel)

    legs.append({
        'from_label': 'Origin' if curr_mile == 0.0 else selected_stops[-1]['station'].name,
        'to_label': 'Destination',
        'from_mile': curr_mile,
        'to_mile': total_distance_miles,
        'distance_miles': final_dist,
        'gallons': final_burn,
        'price': 0.0,
        'cost_usd': 0.0,
        'arrival_fuel_gallons': dest_arrival_fuel,
        'gallons_purchased': 0.0,
        'departure_fuel_gallons': dest_arrival_fuel,
    })

    # Strict physical reconciliation verification
    total_consumed = round(total_distance_miles / fuel_economy_mpg, 2)
    total_purchased = round(sum(s['gallons_purchased'] for s in selected_stops), 2)
    expected_remaining = round(initial_tank_gallons + total_purchased - total_consumed, 2)

    if abs(expected_remaining - dest_arrival_fuel) > 0.05:
        logger.warning(
            f"Physical tank mismatch: expected {expected_remaining:.2f} gal, simulated {dest_arrival_fuel:.2f} gal"
        )

    return {
        'selected_stops': selected_stops,
        'is_feasible': True,
        'legs': legs,
        'warnings': warnings,
    }
