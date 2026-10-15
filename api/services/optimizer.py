import math
import logging
from typing import List, Dict, Any
from scipy.optimize import linprog

logger = logging.getLogger(__name__)

# Floating-point numerical tolerance for hardware IEEE 754 precision limits
NUMERICAL_TOLERANCE = 1e-4


class InfeasibleRouteError(Exception):
    """Raised when destination cannot be reached within vehicle range or fuel constraints."""
    pass


def _is_invalid_number(val: Any) -> bool:
    """Checks whether a value is non-numeric, NaN, or infinite."""
    return not isinstance(val, (int, float)) or not math.isfinite(val)


def get_station_price(station: Any, price_policy: str = 'min') -> float:
    """
    Returns the retail fuel price per gallon for a station according to the requested price policy.
    Supports:
      - 'min': lowest reported price variant (default)
      - 'max': highest reported price variant
      - 'avg': mean of reported price variants
    Gracefully falls back to primary_price or min_price for single-variant stations or test fixtures.
    """
    policy = (price_policy or 'min').strip().lower()
    if policy == 'max' and hasattr(station, 'max_price') and station.max_price is not None:
        return float(station.max_price)
    elif policy == 'avg' and hasattr(station, 'avg_price') and station.avg_price is not None:
        return float(station.avg_price)
    elif policy == 'min' and hasattr(station, 'min_price') and station.min_price is not None:
        return float(station.min_price)
    elif hasattr(station, 'primary_price') and station.primary_price is not None:
        return float(station.primary_price)
    elif hasattr(station, 'min_price') and station.min_price is not None:
        return float(station.min_price)
    raise ValueError(f"Candidate station '{getattr(station, 'name', 'Unknown')}' has no valid price.")


def validate_optimizer_inputs(
    total_distance_miles: float,
    candidate_stops: List[Dict[str, Any]],
    max_range_miles: float,
    fuel_economy_mpg: float,
    tank_capacity_gallons: float,
    initial_tank_gallons: float,
    initial_fuel_price_estimate: float,
    price_policy: str = 'min',
) -> None:
    """
    Validates physical feasibility, parameter bounds, and numeric finiteness before optimization.
    Raises ValueError on non-finite, out-of-bounds, or physically contradictory inputs.
    """
    if _is_invalid_number(total_distance_miles) or total_distance_miles < 0:
        raise ValueError(f"Route distance must be a finite non-negative number (got {total_distance_miles}).")

    if _is_invalid_number(fuel_economy_mpg) or fuel_economy_mpg <= 0:
        raise ValueError(f"Fuel economy must be a finite positive number (got {fuel_economy_mpg}).")

    if _is_invalid_number(tank_capacity_gallons) or tank_capacity_gallons <= 0:
        raise ValueError(f"Tank capacity must be a finite positive number (got {tank_capacity_gallons}).")

    if _is_invalid_number(max_range_miles) or max_range_miles <= 0:
        raise ValueError(f"Maximum range must be a finite positive number (got {max_range_miles}).")

    if _is_invalid_number(initial_tank_gallons) or initial_tank_gallons < 0:
        raise ValueError(f"Starting fuel must be a finite non-negative number (got {initial_tank_gallons}).")

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

    if _is_invalid_number(initial_fuel_price_estimate) or initial_fuel_price_estimate <= 0:
        raise ValueError(
            f"Reference fuel price estimate must be a finite positive number (got {initial_fuel_price_estimate})."
        )

    # Validate candidate station positions and prices
    for i, s in enumerate(candidate_stops):
        if 'mile_marker' not in s or 'station' not in s:
            raise ValueError(f"Candidate stop at index {i} is missing 'mile_marker' or 'station'.")

        mile_m = s['mile_marker']
        if _is_invalid_number(mile_m):
            raise ValueError(f"Candidate stop at index {i} has non-finite mile marker ({mile_m}).")
        if mile_m < 0:
            raise ValueError(f"Candidate stop at index {i} has negative mile marker ({mile_m}).")

        station = s['station']
        price = get_station_price(station, price_policy)
        if _is_invalid_number(price) or price <= 0:
            raise ValueError(
                f"Candidate station '{getattr(station, 'name', i)}' has non-positive or non-finite fuel price ({price})."
            )


def optimize_fuel_stops(
    total_distance_miles: float,
    candidate_stops: List[Dict[str, Any]],
    max_range_miles: float = 500.0,
    fuel_economy_mpg: float = 10.0,
    tank_capacity_gallons: float = 50.0,
    initial_tank_gallons: float = 50.0,
    initial_fuel_price_estimate: float = 3.50,
    price_policy: str = 'min',
) -> Dict[str, Any]:
    """
    Optimizes refueling stops along a driving route to minimize cash fuel expenditure
    while strictly enforcing tank capacity and maximum vehicle range (500 miles).

    Guarantees:
      1. Validates all inputs against physical bounds and numeric finiteness (rejecting NaN/inf).
      2. Departure with starting fuel level in tank (no cash paid at departure).
      3. Validates full-precision LP solution before quantization.
      4. Corrects any rounding deficit strictly at reachable prior stations with spare capacity.
      5. Re-simulates quantized purchases without artificial clamping; rejects any
         material violation (> 1e-4 gal) with InfeasibleRouteError.
      6. Correctly handles cheap-station top-offs, passes stops when leftover fuel suffices,
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
        price_policy=price_policy,
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

    # 3. Deliberately filter candidates strictly between origin and destination:
    # Stations at mile 0.0 (origin) or >= total_distance_miles (destination) cannot serve
    # as intermediate en-route refueling stops and are excluded.
    valid_candidates = [
        s for s in candidate_stops
        if 0.0 < s['mile_marker'] < total_distance_miles
    ]

    if not valid_candidates:
        raise InfeasibleRouteError(
            f"Trip distance ({total_distance_miles:.1f} miles) exceeds departure vehicle range "
            f"({initial_reach:.0f} miles), but zero fuel stations were found along the route corridor."
        )

    # 4. Deduplicate stations at identical mile markers, retaining the cheapest option under active policy
    by_mile = {}
    for s in valid_candidates:
        m = round(s['mile_marker'], 2)
        price = get_station_price(s['station'], price_policy)
        if m not in by_mile or price < get_station_price(by_mile[m]['station'], price_policy):
            by_mile[m] = s

    unique_candidates = sorted(by_mile.values(), key=lambda s: s['mile_marker'])

    # 5. Feasibility checks
    first_station_mile = unique_candidates[0]['mile_marker']
    if first_station_mile > initial_reach:
        raise InfeasibleRouteError(
            f"Cannot complete route: First available station is at mile {first_station_mile:.1f}, "
            f"which exceeds departure vehicle range of {initial_reach:.0f} miles."
        )

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
    prices = [get_station_price(s['station'], price_policy) for s in unique_candidates]
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

    # 8. Quantize Purchases to 2 Decimal Places and Correct Deficits ONLY at Reachable Prior Stations
    quantized_buys = [round(float(b), 2) if b >= 0.01 else 0.0 for b in raw_buys]

    # Iterative backward correction: if arrival at planned stop k (or destination) has a deficit,
    # fuel MUST be added strictly at an earlier reachable planned stop prev_k < k with spare tank headroom.
    max_passes = 10
    for pass_num in range(max_passes):
        planned = [
            (i, unique_candidates[i], quantized_buys[i])
            for i in range(M) if quantized_buys[i] >= 0.01
        ]
        chk_mile = 0.0
        chk_fuel_exact = float(initial_tank_gallons)
        dep_fuels_exact = []
        deficit_found = False

        for plan_k, (cand_idx, cand, b) in enumerate(planned):
            stop_mile = cand['mile_marker']
            dist = stop_mile - chk_mile
            burn_exact = dist / fuel_economy_mpg
            raw_arrival = chk_fuel_exact - burn_exact

            if raw_arrival < -NUMERICAL_TOLERANCE:
                deficit = max(0.01, round(abs(raw_arrival) + 0.005, 2))
                # Search strictly backwards for an earlier planned stop prev_k < plan_k with available headroom
                adjusted = False
                for prev_k in range(plan_k - 1, -1, -1):
                    prev_dep = dep_fuels_exact[prev_k]
                    headroom = tank_capacity_gallons - prev_dep
                    if headroom >= 0.01:
                        add_amt = min(deficit, round(headroom, 2))
                        prev_cand_idx = planned[prev_k][0]
                        quantized_buys[prev_cand_idx] = round(quantized_buys[prev_cand_idx] + add_amt, 2)
                        adjusted = True
                        break
                if not adjusted:
                    raise InfeasibleRouteError(
                        f"Rounding deficit of {deficit:.4f} gal before reaching station "
                        f"'{cand['station'].name}' at mile {stop_mile:.1f} cannot be remedied "
                        f"at any prior station without exceeding tank capacity."
                    )
                deficit_found = True
                break  # Re-simulate from beginning with updated purchase

            raw_departure = raw_arrival + b
            if raw_departure > tank_capacity_gallons + NUMERICAL_TOLERANCE:
                overflow = max(0.01, round(raw_departure - tank_capacity_gallons + 0.005, 2))
                quantized_buys[cand_idx] = max(0.0, round(quantized_buys[cand_idx] - overflow, 2))
                deficit_found = True
                break
            dep_fuels_exact.append(min(tank_capacity_gallons, raw_departure))
            chk_fuel_exact = min(tank_capacity_gallons, raw_departure)
            chk_mile = stop_mile

        if deficit_found:
            continue

        # Check destination arrival
        final_dist = total_distance_miles - chk_mile
        final_burn_exact = final_dist / fuel_economy_mpg
        raw_dest = chk_fuel_exact - final_burn_exact
        if raw_dest < -NUMERICAL_TOLERANCE:
            deficit = max(0.01, round(abs(raw_dest) + 0.005, 2))
            adjusted = False
            for prev_k in range(len(planned) - 1, -1, -1):
                prev_dep = dep_fuels_exact[prev_k]
                headroom = tank_capacity_gallons - prev_dep
                if headroom >= 0.01:
                    add_amt = min(deficit, round(headroom, 2))
                    prev_cand_idx = planned[prev_k][0]
                    quantized_buys[prev_cand_idx] = round(quantized_buys[prev_cand_idx] + add_amt, 2)
                    adjusted = True
                    break
            if not adjusted:
                raise InfeasibleRouteError(
                    f"Rounding deficit of {deficit:.4f} gal at Destination cannot be remedied "
                    f"at any prior station without exceeding tank capacity."
                )
            continue

        # Feasible with no deficits
        break

    # 9. Re-simulate and Re-validate the Quantized Plan Without Clamping
    planned_stops = []
    for i, b in enumerate(quantized_buys):
        if b >= 0.01:
            planned_stops.append((unique_candidates[i], b))

    curr_mile = 0.0
    curr_fuel_exact = float(initial_tank_gallons)
    selected_stops = []
    legs = []

    for cand, gallons_to_buy in planned_stops:
        station = cand['station']
        st_price = get_station_price(station, price_policy)
        stop_mile = cand['mile_marker']
        dist = stop_mile - curr_mile

        if dist > max_range_miles + NUMERICAL_TOLERANCE:
            raise InfeasibleRouteError(f"Driving leg of {dist:.1f} miles exceeds maximum vehicle range.")

        burn_exact = dist / fuel_economy_mpg
        raw_arrival_fuel = curr_fuel_exact - burn_exact

        # Validate raw calculated value against numerical tolerance (no clamping!)
        if raw_arrival_fuel < -NUMERICAL_TOLERANCE:
            raise InfeasibleRouteError(
                f"Fuel ran out (deficit of {-raw_arrival_fuel:.4f} gal) before reaching "
                f"station '{station.name}' at mile {stop_mile:.1f}."
            )

        raw_departure_fuel = raw_arrival_fuel + gallons_to_buy
        if raw_departure_fuel > tank_capacity_gallons + NUMERICAL_TOLERANCE:
            raise InfeasibleRouteError(
                f"Refueling {gallons_to_buy:.2f} gal at '{station.name}' exceeds tank capacity "
                f"({raw_departure_fuel:.4f} gal > {tank_capacity_gallons:.1f} gal)."
            )

        # Update exact continuous fuel for next leg
        curr_fuel_exact = min(tank_capacity_gallons, raw_departure_fuel)

        # Normalize only insignificant floating-point error after validation
        arrival_fuel = 0.0 if abs(raw_arrival_fuel) <= NUMERICAL_TOLERANCE else round(raw_arrival_fuel, 2)
        departure_fuel = (
            tank_capacity_gallons
            if abs(curr_fuel_exact - tank_capacity_gallons) <= NUMERICAL_TOLERANCE
            else round(curr_fuel_exact, 2)
        )

        cost_at_station = round(gallons_to_buy * st_price, 2)

        legs.append({
            'from_label': 'Origin' if len(legs) == 0 else legs[-1]['to_label'],
            'to_label': station.name,
            'from_mile': round(curr_mile, 2),
            'to_mile': round(stop_mile, 2),
            'distance_miles': round(dist, 2),
            'gallons': round(burn_exact, 2),
            'price': st_price,
            'cost_usd': cost_at_station,
            'arrival_fuel_gallons': arrival_fuel,
            'gallons_purchased': gallons_to_buy,
            'departure_fuel_gallons': departure_fuel,
        })

        selected_stops.append({
            'station': station,
            'mile_marker': round(stop_mile, 2),
            'corridor_distance_miles': cand.get('corridor_distance_miles', 0.0),
            'price_per_gallon': st_price,
            'gallons_purchased': gallons_to_buy,
            'cost_usd': cost_at_station,
            'arrival_fuel_gallons': arrival_fuel,
            'departure_fuel_gallons': departure_fuel,
        })

        curr_mile = stop_mile

    final_dist = total_distance_miles - curr_mile
    if final_dist > max_range_miles + NUMERICAL_TOLERANCE:
        raise InfeasibleRouteError(f"Final driving leg of {final_dist:.1f} miles exceeds maximum vehicle range.")

    final_burn_exact = final_dist / fuel_economy_mpg
    raw_dest_fuel = curr_fuel_exact - final_burn_exact

    if raw_dest_fuel < -NUMERICAL_TOLERANCE:
        raise InfeasibleRouteError(
            f"Fuel ran out (deficit of {-raw_dest_fuel:.4f} gal) before reaching Destination "
            f"at mile {total_distance_miles:.1f}."
        )

    # Reconciled destination fuel strictly matches trip-level physical conservation
    total_fuel_consumed = round(total_distance_miles / fuel_economy_mpg, 2)
    total_purchased = round(sum(b for _, b in planned_stops), 2)
    reconciled_dest_fuel = round(initial_tank_gallons + total_purchased - total_fuel_consumed, 2)

    dest_arrival_fuel = (
        0.0 if abs(raw_dest_fuel) <= NUMERICAL_TOLERANCE
        else reconciled_dest_fuel if abs(raw_dest_fuel - reconciled_dest_fuel) <= 0.02
        else round(raw_dest_fuel, 2)
    )

    legs.append({
        'from_label': 'Origin' if len(legs) == 0 else selected_stops[-1]['station'].name,
        'to_label': 'Destination',
        'from_mile': round(curr_mile, 2),
        'to_mile': round(total_distance_miles, 2),
        'distance_miles': round(final_dist, 2),
        'gallons': round(final_burn_exact, 2),
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
