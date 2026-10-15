import unittest
from types import SimpleNamespace
from api.services.accounting import calculate_fuel_accounting


class FuelAccountingTests(unittest.TestCase):
    """
    Unit tests for fuel consumption and economic accounting calculations.
    """

    def make_mock_stop(self, opis_id, name, mile, gallons, price):
        station = SimpleNamespace(
            opis_id=opis_id,
            name=name,
            address="Interstate Exit",
            city="City",
            state="TX",
            primary_price=price,
            min_price=price,
            max_price=price,
            avg_price=price,
            price_variants=[price],
            latitude=31.0,
            longitude=-97.0,
            coordinate_precision="city_corridor_centroid",
            coordinate_source="TEST_FIXTURE",
            highway="I-35",
            exit_number="150",
        )
        return {
            'station': station,
            'mile_marker': mile,
            'corridor_distance_miles': 1.5,
            'gallons_purchased': gallons,
            'price_per_gallon': price,
            'cost_usd': round(gallons * price, 2),
        }

    def test_short_trip_consumption_vs_purchases(self):
        """A short trip burns fuel (24 gal for 240 mi) even if zero fuel is purchased en route."""
        result = calculate_fuel_accounting(
            total_distance_miles=240.0,
            selected_stops=[],
            legs=[{'distance_miles': 240.0, 'gallons': 24.0, 'cost_usd': 84.0}],
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
            reference_fuel_price=3.50,
        )
        self.assertEqual(result['total_fuel_consumed_gallons'], 24.0)
        self.assertEqual(result['fuel_purchased_during_trip_gallons'], 0.0)
        self.assertEqual(result['cash_expenditure_at_pumps_usd'], 0.0)
        # Total trip economic cost accounts for the 24 gallons burned (24 * 3.50 = $84.00)
        self.assertEqual(result['estimated_total_fuel_cost_usd'], 84.0)
        # Initial tank had 50 gal, 24 burned -> 26 gal remaining
        self.assertEqual(result['initial_fuel_accounting']['remaining_fuel_at_destination_gallons'], 26.0)

    def test_multi_stop_accounting_balances(self):
        """On a multi-stop route, fuel purchased plus initial fuel balances with consumption."""
        # 1200 mile trip at 10 MPG = 120 gallons consumed
        # Stop 1 at mile 400 buys 45 gal at $3.00
        # Stop 2 at mile 850 buys 35 gal at $2.90
        stops = [
            self.make_mock_stop(1, "Stop 1", 400.0, 45.0, 3.00),
            self.make_mock_stop(2, "Stop 2", 850.0, 35.0, 2.90),
        ]
        legs = [
            {'distance_miles': 400.0, 'gallons': 40.0, 'cost_usd': 140.0},
            {'distance_miles': 450.0, 'gallons': 45.0, 'cost_usd': 135.0},
            {'distance_miles': 350.0, 'gallons': 35.0, 'cost_usd': 101.5},
        ]
        result = calculate_fuel_accounting(
            total_distance_miles=1200.0,
            selected_stops=stops,
            legs=legs,
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
            reference_fuel_price=3.50,
        )
        self.assertEqual(result['total_fuel_consumed_gallons'], 120.0)
        self.assertEqual(result['fuel_purchased_during_trip_gallons'], 80.0)
        expected_cash = round(45.0 * 3.00 + 35.0 * 2.90, 2)  # 135.0 + 101.5 = 236.50
        self.assertEqual(result['cash_expenditure_at_pumps_usd'], expected_cash)
        self.assertEqual(result['fuel_stops_count'], 2)


if __name__ == '__main__':
    unittest.main()
