from typing import List, Dict, Any


def calculate_fuel_accounting(
    total_distance_miles: float,
    selected_stops: List[Dict[str, Any]],
    legs: List[Dict[str, Any]],
    fuel_economy_mpg: float = 10.0,
    initial_tank_gallons: float = 50.0,
    reference_fuel_price: float = 3.50,
) -> Dict[str, Any]:
    """
    Computes rigorous, separated fuel consumption and economic metrics.

    Separates:
      1. Fuel consumed by vehicle physics (distance / MPG)
      2. Fuel purchased at intermediate stations during trip
      3. Cash out-of-pocket expenditure during trip
      4. Estimated total economic cost of trip (including departure tank fuel)
      5. Final fuel level at destination, strictly reconciled with simulated tank balance
    """
    total_fuel_consumed = round(total_distance_miles / fuel_economy_mpg, 2)

    fuel_purchased_during_trip = 0.0
    expenditure_during_trip = 0.0

    formatted_stops = []
    last_stop_mile = 0.0

    for item in selected_stops:
        st = item['station']
        mile_m = item['mile_marker']
        gallons = item['gallons_purchased']
        cost = item['cost_usd']
        price = item['price_per_gallon']
        dist_from_last = round(mile_m - last_stop_mile, 2)
        last_stop_mile = mile_m

        fuel_purchased_during_trip += gallons
        expenditure_during_trip += cost

        formatted_stops.append({
            'opis_id': st.opis_id,
            'name': st.name,
            'address': st.address,
            'city': st.city,
            'state': st.state,
            'highway': st.highway,
            'exit_number': st.exit_number,
            'coordinates': {
                'latitude': st.latitude,
                'longitude': st.longitude,
                'precision': st.coordinate_precision,
                'source': st.coordinate_source,
                'corridor_offset_miles': item.get('corridor_distance_miles', 0.0),
            },
            'retail_price_per_gallon': price,
            'price_range': [st.min_price, st.max_price],
            'reported_variants_count': len(st.price_variants),
            'mile_marker': mile_m,
            'distance_from_last_stop_miles': dist_from_last,
            'gallons_purchased': gallons,
            'cost_usd': cost,
            'arrival_fuel_gallons': item.get('arrival_fuel_gallons'),
            'departure_fuel_gallons': item.get('departure_fuel_gallons'),
        })

    fuel_purchased_during_trip = round(fuel_purchased_during_trip, 2)
    expenditure_during_trip = round(expenditure_during_trip, 2)

    # Physical reconciliation: Remaining = Initial + Purchased - Consumed
    reconciled_dest_fuel = round(initial_tank_gallons + fuel_purchased_during_trip - total_fuel_consumed, 2)

    # Initial departure fuel burned: portion of initial tank consumed
    initial_fuel_burned = round(min(initial_tank_gallons, total_fuel_consumed), 2)
    initial_fuel_cost = round(initial_fuel_burned * reference_fuel_price, 2)
    estimated_total_fuel_cost = round(expenditure_during_trip + initial_fuel_cost, 2)

    # Validate that remaining fuel does not violate physical boundaries (no hiding errors via clamping)
    if reconciled_dest_fuel < -0.05:
        raise ValueError(f"Impossible negative fuel balance at destination: {reconciled_dest_fuel:.2f} gal")

    return {
        'total_distance_miles': total_distance_miles,
        'fuel_economy_mpg': fuel_economy_mpg,
        'total_fuel_consumed_gallons': total_fuel_consumed,
        'fuel_purchased_during_trip_gallons': fuel_purchased_during_trip,
        'cash_expenditure_at_pumps_usd': expenditure_during_trip,
        'estimated_total_fuel_cost_usd': estimated_total_fuel_cost,
        'initial_fuel_accounting': {
            'starting_tank_gallons': initial_tank_gallons,
            'initial_fuel_burned_gallons': initial_fuel_burned,
            'initial_fuel_cost_valuation_usd': initial_fuel_cost,
            'reference_price_per_gallon': reference_fuel_price,
            'remaining_fuel_at_destination_gallons': reconciled_dest_fuel,
        },
        'fuel_stops_count': len(formatted_stops),
        'fuel_stops': formatted_stops,
        'route_legs': legs,
    }
