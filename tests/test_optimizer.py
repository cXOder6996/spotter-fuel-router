import unittest
from types import SimpleNamespace
from api.services.optimizer import (
    optimize_fuel_stops,
    validate_optimizer_inputs,
    InfeasibleRouteError,
)


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
        """Trips under starting fuel range require 0 intermediate fuel stops."""
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
        self.assertNotIn(1, selected_ids)
        self.assertIn(2, selected_ids)

        stop_b = result['selected_stops'][0]
        self.assertEqual(stop_b['station'].opis_id, 2)
        self.assertEqual(stop_b['gallons_purchased'], 20.0)
        self.assertEqual(stop_b['cost_usd'], round(20.0 * 2.80, 2))

    def test_cheap_station_fills_up_capacity_over_bare_minimum(self):
        """
        Genuinely tests the trade-off between topping off to capacity at a cheap station
        vs. buying only the bare minimum for the immediate leg before an expensive station.

        Setup:
          - Trip distance: 1,000 miles.
          - Initial fuel: 50 gal.
          - Station 1 at mile 400 (price $2.00/gal - cheap).
          - Station 2 at mile 700 (price $4.50/gal - expensive).
          - Destination at mile 1,000.
          - Since Station 1 to Destination is 600 miles (> 500 max range), stopping at Station 2 is mandatory!

        Alternative Strategy (Bare Minimum):
          - At Station 1 (400 mi), arrives with 10 gal. Buys 20 gal to reach Station 2 (300 mi = 30 gal).
            Cost = 20 * $2.00 = $40.00.
          - At Station 2 (700 mi), arrives with 0 gal. Must buy 30 gal to reach Destination (300 mi = 30 gal).
            Cost = 30 * $4.50 = $135.00.
          - Total cash = $40.00 + $135.00 = $175.00.

        Optimal Strategy (Fill up to 50 gal capacity at Station 1):
          - At Station 1, buys 40 gal @ $2.00 = $80.00 -> leaves with 50 gal.
          - Drives 300 mi to Station 2, arrives with 50 - 30 = 20 gal!
          - At Station 2, only needs 30 - 20 = 10 gal to finish the trip!
            Cost = 10 * $4.50 = $45.00.
          - Total cash = $80.00 + $45.00 = $125.00.
          - Savings = $50.00!
        """
        candidates = [
            self.make_mock_station(1, "Cheap Fillup", 400.0, 2.00),
            self.make_mock_station(2, "Expensive Downstream", 700.0, 4.50),
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

        # Station 1: must fill to full (buy 40 gal)
        self.assertEqual(stops[0]['station'].opis_id, 1)
        self.assertEqual(stops[0]['gallons_purchased'], 40.0)
        self.assertEqual(stops[0]['cost_usd'], 80.00)

        # Station 2: only buy the remaining 10 gal
        self.assertEqual(stops[1]['station'].opis_id, 2)
        self.assertEqual(stops[1]['gallons_purchased'], 10.0)
        self.assertEqual(stops[1]['cost_usd'], 45.00)

        total_cash = sum(s['cost_usd'] for s in stops)
        bare_minimum_cost = 175.00
        self.assertEqual(total_cash, 125.00)
        self.assertLess(total_cash, bare_minimum_cost)
        self.assertEqual(bare_minimum_cost - total_cash, 50.00)

    def test_locally_cheap_choice_avoided_for_globally_cheaper_plan(self):
        """
        Tests that the optimizer coordinates fuel purchases across stations to minimize
        total cash expenditure, verified against two independent feasible alternative plans:

        Setup:
          - Trip distance: 800 miles at 10 MPG (consumes 80.0 gal).
          - Starting fuel: 50.0 gal (deficit: 30.0 gal to buy).
          - Station 1 at mile 200 (price $2.80/gal - early cheap station).
          - Station 2 at mile 450 (price $2.90/gal - reachable bridge station).
          - Station 3 at mile 600 (price $4.50/gal - expensive trap station).
          - Destination at mile 800.

        Feasible Alternative Plan 1 (Myopic local greedy - fill up at Station 1, trapped into Station 3):
          - Fills up with 30 gal @ $2.80 = $84.00 at Station 1.
          - Max reach from Station 1 on full tank is mile 700 (cannot reach mile 800 destination).
          - Forced to refuel at Station 3 (mile 600): arrives with 10 gal, buys 10 gal @ $4.50 = $45.00.
          - Total cash for Plan 1 = $84.00 + $45.00 = $129.00.

        Feasible Alternative Plan 2 (Single-stop strategy - skip Station 1, refuel only at Station 2):
          - Arrives at Station 2 (mile 450) with 5 gal.
          - Buys 30 gal @ $2.90 = $87.00. Reaches destination directly.
          - Total cash for Plan 2 = $87.00.

        Globally Optimal LP Plan:
          - Buys 20.0 gal @ $2.80 = $56.00 at Station 1 (leaving with 50.0 gal full tank).
          - Arrives at Station 2 with 25.0 gal. Buys 10.0 gal @ $2.90 = $29.00 (leaving with 35.0 gal).
          - Reaches Destination directly without stopping at Station 3!
          - Total cash for Globally Optimal Plan = $56.00 + $29.00 = $85.00.
          - Strictly cheaper than Alternative Plan 1 ($129.00) and Alternative Plan 2 ($87.00).
        """
        candidates = [
            self.make_mock_station(1, "Locally Cheap Early A", 200.0, 2.80),
            self.make_mock_station(2, "Slightly Higher Reachable B", 450.0, 2.90),
            self.make_mock_station(3, "Expensive Trap C", 600.0, 4.50),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=800.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
        )
        self.assertTrue(result['is_feasible'])
        stops = result['selected_stops']
        stop_ids = [s['station'].opis_id for s in stops]

        # Station 3 (expensive trap) must be completely bypassed
        self.assertNotIn(3, stop_ids)
        self.assertEqual(stop_ids, [1, 2])

        # Verify exact purchase amounts
        self.assertEqual(stops[0]['gallons_purchased'], 20.0)
        self.assertEqual(stops[0]['cost_usd'], 56.00)

        self.assertEqual(stops[1]['gallons_purchased'], 10.0)
        self.assertEqual(stops[1]['cost_usd'], 29.00)

        total_cash = sum(s['cost_usd'] for s in stops)
        self.assertEqual(total_cash, 85.00)

        alternative_plan_1_cost = 129.00
        alternative_plan_2_cost = 87.00

        self.assertLess(total_cash, alternative_plan_1_cost)
        self.assertLess(total_cash, alternative_plan_2_cost)
        self.assertEqual(alternative_plan_2_cost - total_cash, 2.00)
        self.assertEqual(alternative_plan_1_cost - total_cash, 44.00)

    def test_infeasible_route_gap_exceeds_max_range(self):
        """Raises InfeasibleRouteError when distance between consecutive opportunities > 500 miles."""
        candidates = [
            self.make_mock_station(1, "Stop 1", 200.0, 3.00),
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
        Validates fuel balance and tank constraints across all legs without clamping:
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

    def test_invalid_optimizer_input_parameters(self):
        """Verifies that invalid input parameter combinations raise ValueError."""
        mock_cand = [self.make_mock_station(1, "Stop 1", 200.0, 3.00)]

        # Negative distance
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=-100.0, candidate_stops=mock_cand)

        # Non-positive MPG
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=mock_cand, fuel_economy_mpg=0.0)

        # Non-positive tank capacity
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=mock_cand, tank_capacity_gallons=-50.0)

        # Non-positive max range
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=mock_cand, max_range_miles=0.0)

        # Negative starting fuel
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=mock_cand, initial_tank_gallons=-10.0)

        # Starting fuel exceeds tank capacity
        with self.assertRaises(ValueError):
            optimize_fuel_stops(
                total_distance_miles=500.0,
                candidate_stops=mock_cand,
                tank_capacity_gallons=50.0,
                initial_tank_gallons=60.0,
            )

        # Max range exceeds tank capacity * MPG physics
        with self.assertRaises(ValueError):
            optimize_fuel_stops(
                total_distance_miles=500.0,
                candidate_stops=mock_cand,
                tank_capacity_gallons=50.0,
                fuel_economy_mpg=10.0,
                max_range_miles=600.0,  # 600 > 50 * 10
            )

        # Non-positive station price
        bad_price_station = self.make_mock_station(2, "Free Station", 250.0, 0.0)
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=[bad_price_station])

        # Non-finite inputs: NaN and Inf
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=float('nan'), candidate_stops=mock_cand)

        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=mock_cand, fuel_economy_mpg=float('inf'))

        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=mock_cand, initial_tank_gallons=float('nan'))

        nan_price_station = self.make_mock_station(3, "NaN Price Station", 250.0, float('nan'))
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=[nan_price_station])

        nan_mile_station = self.make_mock_station(4, "NaN Mile Station", float('nan'), 3.00)
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=[nan_mile_station])

        neg_mile_station = self.make_mock_station(5, "Negative Mile Station", -25.0, 3.00)
        with self.assertRaises(ValueError):
            optimize_fuel_stops(total_distance_miles=500.0, candidate_stops=[neg_mile_station])

    def test_starting_fuel_below_capacity(self):
        """
        Tests that starting with less than a full tank (e.g. 20 gal = 200 mi range)
        properly constrains the initial reach and forces an early refueling stop.
        """
        candidates = [
            self.make_mock_station(1, "Early Stop", 150.0, 3.20),
            self.make_mock_station(2, "Late Stop", 350.0, 2.90),
        ]
        # Trip of 600 miles starting with only 20 gallons (cannot reach Late Stop at 350 mi)
        result = optimize_fuel_stops(
            total_distance_miles=600.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
            initial_tank_gallons=20.0,  # 200-mile initial range
        )
        self.assertTrue(result['is_feasible'])
        stop_ids = [s['station'].opis_id for s in result['selected_stops']]
        # Must refuel at Early Stop (150 mi) before running out of the 20 gal (200 mi range)
        self.assertIn(1, stop_ids)

    def test_rounding_deficit_corrected_strictly_at_prior_reachable_stop_with_headroom(self):
        """
        Regression test: When LP fractional purchases (e.g. 6.819 gal, 11.678 gal)
        are quantized to 2 decimal places, rounding down could produce a pre-arrival
        deficit (e.g. -0.0010 gal) at a subsequent stop.
        Verifies:
          1. The deficit is corrected strictly at an earlier reachable stop with spare headroom.
          2. The plan is re-simulated from origin after adjustment.
          3. Departure fuel never exceeds tank capacity (<= 50.0 gal) at any station.
          4. Arrival fuel is nonnegative on every leg and at destination.
        """
        candidates = [
            self.make_mock_station(1, "Louisville Stop", 296.48, 2.9057),
            self.make_mock_station(2, "Chattanooga Stop", 568.19, 2.7857),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=684.97,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
            tank_capacity_gallons=50.0,
            initial_tank_gallons=50.0,
        )
        self.assertTrue(result['is_feasible'])
        self.assertEqual(len(result['selected_stops']), 2)

        stop1 = result['selected_stops'][0]
        stop2 = result['selected_stops'][1]

        # Stop 1 must receive the backward-corrected purchase (6.83 gal instead of 6.82 gal)
        self.assertEqual(stop1['gallons_purchased'], 6.83)
        self.assertLessEqual(stop1['departure_fuel_gallons'], 50.0)
        self.assertGreaterEqual(stop1['arrival_fuel_gallons'], 0.0)

        # Stop 2 arrives with non-negative fuel due to earlier correction
        self.assertEqual(stop2['gallons_purchased'], 11.68)
        self.assertLessEqual(stop2['departure_fuel_gallons'], 50.0)
        self.assertGreaterEqual(stop2['arrival_fuel_gallons'], 0.0)

        # Destination leg arrives with positive fuel
        final_leg = result['legs'][-1]
        self.assertGreaterEqual(final_leg['arrival_fuel_gallons'], 0.0)


if __name__ == '__main__':
    unittest.main()
