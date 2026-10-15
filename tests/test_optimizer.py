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
        """Trips under 500 miles require 0 intermediate fuel stops."""
        result = optimize_fuel_stops(
            total_distance_miles=240.0,
            candidate_stops=[self.make_mock_station(1, "Station A", 100.0, 3.10)],
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
        )
        self.assertTrue(result['is_feasible'])
        self.assertEqual(len(result['selected_stops']), 0)
        self.assertEqual(len(result['legs']), 1)
        self.assertEqual(result['legs'][0]['distance_miles'], 240.0)

    def test_single_intermediate_stop_for_medium_route(self):
        """An 800-mile trip requires at least 1 intermediate stop within 500-mile range."""
        candidates = [
            self.make_mock_station(1, "Stop 1", 350.0, 3.00),
            self.make_mock_station(2, "Stop 2", 450.0, 2.90),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=800.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
        )
        self.assertTrue(result['is_feasible'])
        self.assertEqual(len(result['selected_stops']), 1)
        # Should pick Stop 2 at mile 450 because it's cheaper (2.90 vs 3.00) and within 500 miles of both 0 and 800
        stop = result['selected_stops'][0]
        self.assertEqual(stop['station'].opis_id, 2)
        self.assertEqual(stop['mile_marker'], 450.0)
        # Legs: 0->450 (450 mi <= 500), 450->800 (350 mi <= 500)
        self.assertEqual(len(result['legs']), 2)
        for leg in result['legs']:
            self.assertLessEqual(leg['distance_miles'], 500.0)

    def test_multiple_refueling_stops_on_long_route(self):
        """A 1,400-mile route requires multiple stops, each leg <= 500 miles."""
        candidates = [
            self.make_mock_station(1, "Stop 1", 400.0, 3.20),
            self.make_mock_station(2, "Stop 2", 800.0, 3.10),
            self.make_mock_station(3, "Stop 3", 1100.0, 3.05),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=1400.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
        )
        self.assertTrue(result['is_feasible'])
        # Must have at least 2 stops (e.g. 0->400, 400->800, 800->1100 or 1400)
        self.assertGreaterEqual(len(result['selected_stops']), 2)
        for leg in result['legs']:
            self.assertLessEqual(leg['distance_miles'], 500.0)

    def test_cheapest_station_ahead_preference(self):
        """The algorithm prefers cheaper stations ahead over expensive ones."""
        candidates = [
            self.make_mock_station(1, "Expensive Near", 350.0, 4.50),
            self.make_mock_station(2, "Cheap Reachable", 450.0, 2.75),
            self.make_mock_station(3, "Moderate Far", 850.0, 3.20),
        ]
        result = optimize_fuel_stops(
            total_distance_miles=1200.0,
            candidate_stops=candidates,
            max_range_miles=500.0,
            fuel_economy_mpg=10.0,
        )
        self.assertTrue(result['is_feasible'])
        stop_ids = [s['station'].opis_id for s in result['selected_stops']]
        # Should pick Station 2 (mile 450, cheap) and skip Station 1 (mile 350, expensive)
        self.assertIn(2, stop_ids)
        self.assertNotIn(1, stop_ids)

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
