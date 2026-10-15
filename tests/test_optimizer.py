import unittest
from types import SimpleNamespace
from api.services.optimizer import optimize_fuel_stops, InfeasibleRouteError


class FuelOptimizerTests(unittest.TestCase):
    """
    Unit tests for the refueling optimization algorithm.
    Runs entirely isolated from databases and external APIs.
    """

    def make_mock_station(self, opis_id, name, mile_marker, price):
        station = SimpleNamespace(
            opis_id=opis_id,
            name=name,
            address="Interstate",
            city="City",
            state="ST",
            primary_price=price,
            min_price=price,
            max_price=price,
            avg_price=price,
            price_variants=[price],
            latitude=35.0,
            longitude=-97.0,
            coordinate_precision="city_corridor_centroid",
            coordinate_source="TEST_FIXTURE",
            highway="I-40",
            exit_number="100",
        )
        return {
            'station': station,
            'mile_marker': mile_marker,
            'corridor_distance_miles': 2.0,
        }

    def test_short_trip_requires_no_stops(self):
        """Trips under 500 miles require 0 intermediate fuel stops and depart on starting fuel."""
        result = optimize_fuel_stops(
            total_distance_miles=240.0,
            candidate_stops=[self.make_mock_station(1, "Station A", 100.0, 3.10)],
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
        )
        self.assertTrue(result['is_feasible'])
        self.assertEqual(len(result['selected_stops']), 0)
        self.assertEqual(len(result['legs']), 1)
        leg = result['legs'][0]
        self.assertEqual(leg['distance_miles'], 240.0)
        self.assertEqual(leg['cost_usd'], 0.0)
        self.assertEqual(leg['departure_fuel_gallons'], 50.0)
        self.assertEqual(leg['arrival_fuel_gallons'], 26.0)

    def test_stop_reached_with_leftover_fuel_exceeding_next_leg_requirement(self):
        """
        When arriving at an expensive station with ample leftover fuel to reach a cheaper
        station downstream, the optimizer purchases 0 gallons and drives through.
        """
        # Origin (50 gal) -> Station A (mile 200, $3.50) -> Station B (mile 320, $2.80) -> Dest (mile 700)
        # Leg 0->200 consumes 20 gal. Arrival fuel at A = 30 gal.
        # Leg A->B is 120 mi (needs 12 gal). Since 30 gal >= 12 gal, 0 fuel is purchased at Station A!
        candidates = [
            self.make_mock_station(1, "Expensive Intermediate A", 200.0, 3.50),
            self.make_mock_station(2, "Cheap Reachable B", 320.0, 2.80),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=700.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
        )
        self.assertTrue(result['is_feasible'])
        selected_ids = [s['station'].opis_id for s in result['selected_stops']]
        # Must only buy at Station B (id 2) and skip buying at Station A (id 1)
        self.assertNotIn(1, selected_ids)
        self.assertIn(2, selected_ids)

        stop_b = result['selected_stops'][0]
        self.assertEqual(stop_b['station'].opis_id, 2)
        # At mile 320, truck arrived with 50 - 32 = 18.0 gal.
        # Distance to destination is 700 - 320 = 380 mi (needs 38.0 gal).
        # Therefore, truck purchases 38.0 - 18.0 = 20.0 gallons at Station B.
        self.assertEqual(stop_b['gallons_purchased'], 20.0)
        self.assertEqual(stop_b['cost_usd'], round(20.0 * 2.80, 2))

    def test_cheap_station_fills_up_capacity_over_bare_minimum(self):
        """
        At a significantly cheaper station, topping off the tank to capacity (50 gal)
        is superior to buying only enough for the immediate leg, avoiding purchases at an expensive stop.
        """
        # Origin (50 gal).
        # Station 1 at mile 400 (price $2.00/gal - cheap).
        # Station 2 at mile 600 (price $4.50/gal - expensive).
        # Destination at mile 900.
        #
        # If buying bare minimum:
        # Arrives at Station 1 with 10 gal. Buys 10 gal ($20) to reach Station 2 (needs 20 gal).
        # Arrives at Station 2 with 0 gal. Must buy 30 gal @ $4.50 = $135 to reach Dest. Total = $155.
        #
        # If filling up to capacity at Station 1:
        # Arrives at Station 1 with 10 gal. Buys 40 gal ($80) -> departure fuel 50 gal.
        # Drives to Station 2 (200 mi, burns 20 gal) -> arrives at Station 2 with 30 gal!
        # Distance to Dest is 300 mi (needs 30 gal) -> buys 0 gal at Station 2!
        # Total cash = $80.00 (saving $75.00!).
        candidates = [
            self.make_mock_station(1, "Cheap Fillup", 400.0, 2.00),
            self.make_mock_station(2, "Expensive Trap", 600.0, 4.50),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=900.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
        )
        self.assertTrue(result['is_feasible'])
        selected = result['selected_stops']
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['station'].opis_id, 1)
        self.assertEqual(selected[0]['gallons_purchased'], 40.0)
        self.assertEqual(selected[0]['cost_usd'], 80.0)

    def test_locally_cheap_choice_avoided_for_globally_cheaper_plan(self):
        """
        A locally cheaper station that prevents reaching a far-off bargain (or forces an
        expensive emergency stop) is bypassed in favor of a globally optimal refueling plan.
        """
        # Origin (50 gal).
        # Station A at mile 300 ($3.00) - locally cheaper than B.
        # Station B at mile 450 ($3.20).
        # Station C at mile 850 ($2.50) - super cheap downstream.
        # Emergency D at mile 600 ($5.50) - needed if stopping only at A because 850-300=550 > 500 max range.
        # Destination at mile 950.
        candidates = [
            self.make_mock_station(1, "Locally Cheap A", 300.0, 3.00),
            self.make_mock_station(2, "Reachable Bridge B", 450.0, 3.20),
            self.make_mock_station(3, "Emergency D", 600.0, 5.50),
            self.make_mock_station(4, "Deep Bargain C", 850.0, 2.50),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=950.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
        )
        self.assertTrue(result['is_feasible'])
        stop_ids = [s['station'].opis_id for s in result['selected_stops']]
        # The algorithm must not get stuck into buying at Emergency D
        self.assertNotIn(3, stop_ids)
        # Deep Bargain C must be utilized
        self.assertIn(4, stop_ids)

    def test_infeasible_route_gap_exceeds_max_range(self):
        """Raises InfeasibleRouteError when distance between consecutive opportunities > 500 miles."""
        candidates = [
            self.make_mock_station(1, "Stop 1", 200.0, 3.00),
            # Gap from 200 to 750 is 550 miles (> 500 max range)
            self.make_mock_station(2, "Stop 2", 750.0, 3.00),
        ]
        with self.assertRaises(InfeasibleRouteError) as ctx:
            optimize_fuel_stops(
                total_distance_miles=1000.0,
                candidate_stops=candidates,
                max_range_miles=500.0,
            )
        self.assertIn("exceeds the vehicle's maximum driving range", str(ctx.exception))

    def test_fuel_balance_and_tank_capacity_conservation_after_every_leg(self):
        """
        Validates that after every leg:
          1. 0 <= arrival_fuel <= 50.0 gal.
          2. 0 <= departure_fuel <= 50.0 gal.
          3. Leg distance <= 500.0 miles.
          4. Departure fuel == arrival_fuel + gallons_purchased.
          5. Overall trip balance: Initial + Total Purchased - Total Consumed == Destination Arrival Fuel.
        """
        candidates = [
            self.make_mock_station(1, "Stop 1", 350.0, 3.10),
            self.make_mock_station(2, "Stop 2", 750.0, 2.95),
            self.make_mock_station(3, "Stop 3", 1150.0, 3.05),
        ]
        total_dist = 1500.0
        initial_tank = 50.0
        mpg = 10.0
        result = optimize_fuel_stops(
            total_distance_miles=total_dist,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=mpg,
            initial_tank_gallons=initial_tank,
        )
        self.assertTrue(result['is_feasible'])

        simulated_fuel = initial_tank
        total_purchased = 0.0

        for leg in result['legs']:
            dist = leg['distance_miles']
            self.assertLessEqual(dist, 500.0)
            burn = round(dist / mpg, 2)
            simulated_fuel = round(simulated_fuel - burn, 2)

            self.assertGreaterEqual(leg['arrival_fuel_gallons'], -0.01)
            self.assertLessEqual(leg['arrival_fuel_gallons'], 50.0)
            self.assertAlmostEqual(leg['arrival_fuel_gallons'], simulated_fuel, delta=0.05)

            purchased = leg.get('gallons_purchased', 0.0)
            total_purchased += purchased
            simulated_fuel = round(simulated_fuel + purchased, 2)

            self.assertGreaterEqual(leg['departure_fuel_gallons'], -0.01)
            self.assertLessEqual(leg['departure_fuel_gallons'], 50.0)
            self.assertAlmostEqual(leg['departure_fuel_gallons'], simulated_fuel, delta=0.05)

        total_consumed = round(total_dist / mpg, 2)
        expected_dest_fuel = round(initial_tank + total_purchased - total_consumed, 2)
        final_leg = result['legs'][-1]
        self.assertAlmostEqual(final_leg['arrival_fuel_gallons'], expected_dest_fuel, delta=0.05)

    def test_known_optimal_cash_cost_independent_verification(self):
        """
        Closed-form verification:
          - Trip distance: 1,000 miles at 10 MPG -> 100.0 gal total consumption.
          - Initial fuel: 50.0 gal covers 500 miles -> 50.0 gal deficit.
          - Station 1 at mile 400 ($3.50/gal).
          - Station 2 at mile 700 ($3.00/gal).
          - Destination at mile 1000.
          - At Station 1 (400 mi), arrival fuel = 10.0 gal.
            Distance to Station 2 (700 mi) is 300 mi (needs 30.0 gal).
            Because Station 2 is cheaper ($3.00 < $3.50), truck buys only 30.0 - 10.0 = 20.0 gal at Station 1.
          - At Station 2 (700 mi), arrival fuel = 0.0 gal.
            Distance to Destination is 300 mi (needs 30.0 gal).
            Truck buys 30.0 gal at Station 2.
          - Expected cash cost: 20.0 * 3.50 + 30.0 * 3.00 = $70.00 + $90.00 = $160.00.
        """
        candidates = [
            self.make_mock_station(1, "Stop 1", 400.0, 3.50),
            self.make_mock_station(2, "Stop 2", 700.0, 3.00),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=1000.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
        )
        self.assertTrue(result['is_feasible'])
        stops = result['selected_stops']
        self.assertEqual(len(stops), 2)

        self.assertEqual(stops[0]['station'].opis_id, 1)
        self.assertEqual(stops[0]['gallons_purchased'], 20.0)
        self.assertEqual(stops[0]['cost_usd'], 70.0)

        self.assertEqual(stops[1]['station'].opis_id, 2)
        self.assertEqual(stops[1]['gallons_purchased'], 30.0)
        self.assertEqual(stops[1]['cost_usd'], 90.0)

        total_cash = sum(s['cost_usd'] for s in stops)
        self.assertEqual(total_cash, 160.0)

    def test_empty_candidates_on_long_route_raises_error(self):
        """Raises InfeasibleRouteError when route > 500 miles but 0 candidate stations exist."""
        with self.assertRaises(InfeasibleRouteError):
            optimize_fuel_stops(
                total_distance_miles=800.0,
                candidate_stops=[],
                max_range_miles=500.0,
            )


if __name__ == '__main__':
    unittest.main()
