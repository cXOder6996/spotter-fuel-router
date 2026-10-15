import logging
from typing import List, Dict, Any
from scipy.optimize import linprog

logger = logging.getLogger(__name__)

# Floating-point numerical tolerance for hardware IEEE 754 precision limits
NUMERICAL_TOLERANCE = 1e-4


class InfeasibleRouteError(Exception):
    """Raised when destination cannot be reached within vehicle range or fuel constraints."""
    pass


def validate_optimizer_inputs(
    total_distance_miles: float,
    candidate_stops: List[Dict[str, Any]],
    max_range_miles: float,
    fuel_economy_mpg: float,
    tank_capacity_gallons: float,
    initial_tank_gallons: float,
    initial_fuel_price_estimate: float,
) -> None:
    """
    Validates physical feasibility and parameter bounds before optimization.
    Raises ValueError on invalid or physically contradictory inputs.
    """
    if total_distance_miles < 0:
        raise ValueError(f"Route distance cannot be negative (got {total_distance_miles}).")

    if fuel_economy_mpg <= 0:
        raise ValueError(f"Fuel economy must be strictly positive (got {fuel_economy_mpg}).")

    if tank_capacity_gallons <= 0:
        raise ValueError(f"Tank capacity must be strictly positive (got {tank_capacity_gallons}).")

    if max_range_miles <= 0:
        raise ValueError(f"Maximum range must be strictly positive (got {max_range_miles}).")

    if initial_tank_gallons < 0:
        raise ValueError(f"Starting fuel cannot be negative (got {initial_tank_gallons}).")

    if initial_tank_gallons > tank_capacity_gallons + NUMERICAL_TOLERANCE:
        raise ValueError(
            f"Starting fuel ({initial_tank_gallons:.2f} gal) cannot exceed "
            f"tank capacity ({tank_capacity_gallons:.2f} gal)."
        )

    # Physical consistency: vehicle cannot drive further on a full tank than fuel physics allow
    physics_implied_range = tank_capacity_gallons * fuel_economy_mpg
    if max_range_miles > physics_implied_range + NUMERICAL_TOLERANCE:
        raise ValueError(
            f"Configured maximum range ({max_range_miles:.1f} mi) exceeds physical range "
            f"implied by tank capacity and MPG ({physics_implied_range:.1f} mi)."
        )

    if initial_fuel_price_estimate <= 0:
        raise ValueError(
            f"Reference fuel price estimate must be positive (got {initial_fuel_price_estimate})."
        )

    # Validate candidate station objects
    for i, s in enumerate(candidate_stops):
        if 'mile_marker' not in s or 'station' not in s:
            raise ValueError(f"Candidate stop at index {i} is missing 'mile_marker' or 'station'.")
        station = s['station']
        if not hasattr(station, 'primary_price') or station.primary_price <= 0:
            raise ValueError(f"Candidate station '{getattr(station, 'name', i)}' has non-positive fuel price.")
        if s['mile_marker'] < 0:
            raise ValueError(f"Candidate station '{getattr(station, 'name', i)}' has negative mile marker.")


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

    Guarantees:
      1. Validates all inputs against physical constraints and parameter bounds.
      2. Departure with starting fuel level in tank (no cash paid at departure).
      3. Validates full-precision LP solution before quantization.
      4. Re-simulates quantized purchases without artificial clamping; rejects any
         material violation (> 1e-4 gal) with InfeasibleRouteError.
      5. Correctly handles cheap-station top-offs, passes stops when leftover fuel suffices,
         and reconciles physical tank balance exactly.
    """
    # 1. Parameter validation
    validate_optimizer_inputs(
        total_distance_miles=total_distance_miles,
        candidate_stops=candidate_stops,
        max_range_miles=max_range_miles,
        fuel_economy_mpg=fuel_economy_mpg,
        tank_capacity_gallons=tank_capacity_gallons,
        initial_tank_gallons=initial_tank_gallons,
        initial_fuel_price_estimate=initial_fuel_price_estimate,
    )

    warnings = []

    # 2. Short trip check: directly reachable with starting fuel
    initial_reach = initial_tank_gallons * fuel_economy_mpg
    if total_distance_miles <= initial_reach:
        direct_gallons = round(total_distance_miles / fuel_economy_mpg, 2)
        remaining_fuel = round(initial_tank_gallons - direct_gallons, 2)

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
                'cost_usd': 0.0,
                'arrival_fuel_gallons': remaining_fuel,
                'gallons_purchased': 0.0,
                'departure_fuel_gallons': initial_tank_gallons,
            }],
            'warnings': ["Trip is within vehicle departure range. No intermediate refueling stop required."],
        }

    # 3. Filter candidates strictly between origin and destination
    valid_candidates = [
        s for s in candidate_stops
        if 0 < s['mile_marker'] < total_distance_miles
    ]

    if not valid_candidates:
        raise InfeasibleRouteError(
            f"Trip distance ({total_distance_miles:.1f} miles) exceeds departure vehicle range "
            f"({initial_reach:.0f} miles), but zero fuel stations were found along the route corridor."
        )

    # 4. Deduplicate stations at identical mile markers, retaining the cheapest option
    by_mile = {}
    for s in valid_candidates:
        m = round(s['mile_marker'], 2)
        price = s['station'].primary_price
        if m not in by_mile or price < by_mile[m]['station'].primary_price:
            by_mile[m] = s

    unique_candidates = sorted(by_mile.values(), key=lambda s: s['mile_marker'])

    # 5. Feasibility checks
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

    # 6. Continuous-Fuel Linear Programming Formulation
    M = len(unique_candidates)
    miles = [s['mile_marker'] for s in unique_candidates]
    prices = [s['station'].primary_price for s in unique_candidates]
    r = 1.0 / fuel_economy_mpg

    A_ub = []
    b_ub = []

    # Lower bound constraints: fuel upon arrival at next point >= 0
    # S_i >= r * x_{i+1} - initial_tank_gallons  =>  -S_i <= initial_tank_gallons - r * x_{i+1}
    for i in range(M):
        next_x = miles[i + 1] if i + 1 < M else total_distance_miles
        row = [-1.0 if k <= i else 0.0 for k in range(M)]
        A_ub.append(row)
        b_ub.append(initial_tank_gallons - r * next_x)

    # Upper bound constraints: fuel after purchase <= tank_capacity_gallons
    # S_i <= tank_capacity_gallons - initial_tank_gallons + r * miles[i]
    for i in range(M):
        row = [1.0 if k <= i else 0.0 for k in range(M)]
        A_ub.append(row)
        b_ub.append(tank_capacity_gallons - initial_tank_gallons + r * miles[i])

    bounds = [(0.0, tank_capacity_gallons) for _ in range(M)]
    res = linprog(prices, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method='highs')

    if not res.success:
        raise InfeasibleRouteError(f"No feasible refueling plan found: {res.message}")

    raw_buys = res.x

    # 7. Validate Full-Precision Solution First
    sim_fuel = initial_tank_gallons
    sim_mile = 0.0
    for i in range(M):
        dist = miles[i] - sim_mile
        sim_fuel -= dist * r
        if sim_fuel < -NUMERICAL_TOLERANCE:
            raise InfeasibleRouteError(f"Full-precision infeasibility at station index {i}.")
        sim_fuel += raw_buys[i]
        if sim_fuel > tank_capacity_gallons + NUMERICAL_TOLERANCE:
            raise InfeasibleRouteError(f"Full-precision capacity violation at station index {i}.")
        sim_mile = miles[i]

    dist_to_dest = total_distance_miles - sim_mile
    sim_fuel -= dist_to_dest * r
    if sim_fuel < -NUMERICAL_TOLERANCE:
        raise InfeasibleRouteError("Full-precision infeasibility reaching destination.")

    # 8. Quantize Purchases to 2 Decimal Places and Correct Any Rounding Deficit
    quantized_buys = [round(float(b), 2) if b >= 0.01 else 0.0 for b in raw_buys]

    # Check if rounding down created a deficit on any leg
    chk_fuel = initial_tank_gallons
    chk_mile = 0.0
    last_purchase_idx = None

    for i in range(M):
        dist = miles[i] - chk_mile
        chk_fuel -= dist * r
        if quantized_buys[i] > 0:
            last_purchase_idx = i
        if chk_fuel < -NUMERICAL_TOLERANCE and last_purchase_idx is not None:
            deficit = round(abs(chk_fuel) + 0.005, 2)
            quantized_buys[last_purchase_idx] = round(quantized_buys[last_purchase_idx] + deficit, 2)
            chk_fuel += deficit
        chk_fuel += quantized_buys[i]
        chk_mile = miles[i]

    final_dist = total_distance_miles - chk_mile
    chk_fuel -= final_dist * r
    if chk_fuel < -NUMERICAL_TOLERANCE and last_purchase_idx is not None:
        deficit = round(abs(chk_fuel) + 0.005, 2)
        quantized_buys[last_purchase_idx] = round(quantized_buys[last_purchase_idx] + deficit, 2)

    # 9. Re-simulate and Re-validate the Quantized Plan Without Clamping
    planned_stops = []
    for i, b in enumerate(quantized_buys):
        if b >= 0.01:
            planned_stops.append((unique_candidates[i], b))

    curr_mile = 0.0
    curr_fuel = initial_tank_gallons
    selected_stops = []
    legs = []

    for cand, gallons_to_buy in planned_stops:
        station = cand['station']
        stop_mile = cand['mile_marker']
        dist = round(stop_mile - curr_mile, 2)

        if dist > max_range_miles + NUMERICAL_TOLERANCE:
            raise InfeasibleRouteError(f"Driving leg of {dist:.1f} miles exceeds maximum vehicle range.")

        burn = round(dist / fuel_economy_mpg, 4)
        raw_arrival_fuel = curr_fuel - burn

        # Validate raw calculated value against numerical tolerance (no clamping!)
        if raw_arrival_fuel < -NUMERICAL_TOLERANCE:
            raise InfeasibleRouteError(
                f"Fuel ran out (deficit of {-raw_arrival_fuel:.4f} gal) before reaching "
                f"station '{station.name}' at mile {stop_mile:.1f}."
            )

        # Normalize only insignificant floating-point error after validation
        arrival_fuel = 0.0 if abs(raw_arrival_fuel) <= NUMERICAL_TOLERANCE else round(raw_arrival_fuel, 2)

        raw_departure_fuel = arrival_fuel + gallons_to_buy
        if raw_departure_fuel > tank_capacity_gallons + NUMERICAL_TOLERANCE:
            raise InfeasibleRouteError(
                f"Refueling {gallons_to_buy:.2f} gal at '{station.name}' exceeds tank capacity "
                f"({raw_departure_fuel:.4f} gal > {tank_capacity_gallons:.1f} gal)."
            )

        departure_fuel = (
            tank_capacity_gallons
            if abs(raw_departure_fuel - tank_capacity_gallons) <= NUMERICAL_TOLERANCE
            else round(raw_departure_fuel, 2)
        )

        cost_at_station = round(gallons_to_buy * station.primary_price, 2)

        legs.append({
            'from_label': 'Origin' if curr_mile == 0.0 else legs[-1]['to_label'],
            'to_label': station.name,
            'from_mile': curr_mile,
            'to_mile': stop_mile,
            'distance_miles': dist,
            'gallons': round(burn, 2),
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
    if final_dist > max_range_miles + NUMERICAL_TOLERANCE:
        raise InfeasibleRouteError(f"Final driving leg of {final_dist:.1f} miles exceeds maximum vehicle range.")

    final_burn = round(final_dist / fuel_economy_mpg, 4)
    raw_dest_fuel = curr_fuel - final_burn

    if raw_dest_fuel < -NUMERICAL_TOLERANCE:
        raise InfeasibleRouteError(
            f"Fuel ran out (deficit of {-raw_dest_fuel:.4f} gal) before reaching Destination "
            f"at mile {total_distance_miles:.1f}."
        )

    dest_arrival_fuel = 0.0 if abs(raw_dest_fuel) <= NUMERICAL_TOLERANCE else round(raw_dest_fuel, 2)

    legs.append({
        'from_label': 'Origin' if curr_mile == 0.0 else selected_stops[-1]['station'].name,
        'to_label': 'Destination',
        'from_mile': curr_mile,
        'to_mile': total_distance_miles,
        'distance_miles': final_dist,
        'gallons': round(final_burn, 2),
        'price': 0.0,
        'cost_usd': 0.0,
        'arrival_fuel_gallons': dest_arrival_fuel,
        'gallons_purchased': 0.0,
        'departure_fuel_gallons': dest_arrival_fuel,
    })

    return {
        'selected_stops': selected_stops,
        'is_feasible': True,
        'legs': legs,
        'warnings': warnings,
    }
