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

    def test_reconciliation_identity_and_no_clamping(self):
        """
        Confirms exact reconciliation:
        starting_tank + fuel_purchased - total_consumed == remaining_fuel
        and raises ValueError on impossible negative remaining fuel rather than clamping.
        """
        stops = [self.make_mock_stop(1, "Stop 1", 300.0, 10.0, 3.00)]
        # Trip of 800 miles at 10 MPG consumes 80 gal.
        # Starting with 50 gal + 10 gal purchased = 60 gal.
        # Deficit of 20 gal -> remaining would be -20 gal!
        # Must raise ValueError, NOT silently clamp to 0.0.
        with self.assertRaises(ValueError) as ctx:
            calculate_fuel_accounting(
                total_distance_miles=800.0,
                selected_stops=stops,
                legs=[],
                fuel_economy_mpg=10.0,
                initial_tank_gallons=50.0,
            )
        self.assertIn("Impossible negative fuel balance", str(ctx.exception))

    def test_fifo_inventory_valuation_and_destination_asset_balance(self):
        """
        Tests that when fuel is purchased but remains in the tank at destination,
        economic cost of fuel consumed only counts fuel actually burned, and the
        remaining fuel is properly accounted for as ending asset inventory.
        """
        stops = [self.make_mock_stop(1, "Stop 1", 450.0, 40.0, 3.00)]
        result = calculate_fuel_accounting(
            total_distance_miles=800.0,
            selected_stops=stops,
            legs=[],
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
            reference_fuel_price=3.50,
        )
        self.assertEqual(result['total_fuel_consumed_gallons'], 80.0)
        self.assertEqual(result['fuel_purchased_during_trip_gallons'], 40.0)
        self.assertEqual(result['cash_expenditure_at_pumps_usd'], 120.00)
        self.assertEqual(result['destination_remaining_fuel_gallons'], 10.0)

        inv = result['inventory_fuel_accounting']
        self.assertEqual(inv['departure_fuel_valuation_usd'], 175.00)
        self.assertEqual(inv['purchased_fuel_burned_cost_usd'], 90.00)
        self.assertEqual(inv['economic_cost_of_consumed_fuel_usd'], 265.00)
        self.assertEqual(inv['destination_inventory_valuation_usd'], 30.00)

        total_input_value = inv['departure_fuel_valuation_usd'] + result['cash_expenditure_at_pumps_usd']
        total_accounted_value = inv['economic_cost_of_consumed_fuel_usd'] + inv['destination_inventory_valuation_usd']
        self.assertEqual(total_input_value, total_accounted_value)

    def test_inventory_conservation_equation_short_route(self):
        """
        Asserts the fundamental inventory conservation equation on a short route:
        Starting Inventory Valuation ($) + Pump Cash Spent ($)
          == Economic Fuel Burned Cost ($) + Destination Inventory Valuation ($)
        Under a $3.50/gal reference price for 50 gal starting tank ($175.00).
        """
        # 239.62 mile trip at 10 MPG consumes 23.96 gal.
        # Starting with 50 gal full tank -> 26.04 gal remaining at destination.
        result = calculate_fuel_accounting(
            total_distance_miles=239.62,
            selected_stops=[],
            legs=[{'distance_miles': 239.62, 'gallons': 23.96, 'cost_usd': 0.0}],
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
            reference_fuel_price=3.50,
        )
        inv = result['inventory_fuel_accounting']
        self.assertEqual(inv['reference_fuel_price_usd_per_gallon'], 3.50)
        self.assertEqual(inv['starting_inventory_valuation_usd'], 175.00)
        self.assertEqual(inv['departure_fuel_burned_gallons'], 23.96)
        self.assertEqual(inv['departure_fuel_valuation_usd'], 83.86)
        self.assertEqual(inv['unused_starting_fuel_at_destination_gallons'], 26.04)
        self.assertEqual(inv['destination_inventory_valuation_usd'], 91.14)
        self.assertEqual(result['cash_expenditure_at_pumps_usd'], 0.00)

        # Total input = 175.00 + 0.00 = 175.00
        # Total output = 83.86 + 91.14 = 175.00
        self.assertEqual(inv['total_input_valuation_usd'], 175.00)
        self.assertEqual(inv['total_output_valuation_usd'], 175.00)
        self.assertTrue(inv['is_inventory_conserved'])

    def test_inventory_conservation_equation_multi_stop_route(self):
        """
        Asserts the inventory conservation equation on a multi-stop route with purchases:
        Starting Inventory Valuation ($) + Pump Cash Spent ($)
          == Economic Fuel Burned Cost ($) + Destination Inventory Valuation ($)
        """
        # 685.0 mile trip at 10 MPG consumes 68.5 gal.
        # Stop 1 buys 6.83 gal @ $2.9057 ($19.85)
        # Stop 2 buys 11.68 gal @ $2.7857 ($32.54)
        # Total purchases = 18.51 gal, Cash spent = $52.39.
        # Reconciled destination fuel = 50.0 + 18.51 - 68.50 = 0.01 gal.
        stops = [
            self.make_mock_stop(1, "Stop 1", 296.48, 6.83, 2.9057),
            self.make_mock_stop(2, "Stop 2", 568.19, 11.68, 2.7857),
        ]
        result = calculate_fuel_accounting(
            total_distance_miles=684.97,
            selected_stops=stops,
            legs=[],
            fuel_economy_mpg=10.0,
            initial_tank_gallons=50.0,
            reference_fuel_price=3.50,
        )
        inv = result['inventory_fuel_accounting']
        self.assertEqual(inv['starting_inventory_valuation_usd'], 175.00)
        self.assertEqual(result['cash_expenditure_at_pumps_usd'], 52.39)
        self.assertEqual(inv['departure_fuel_burned_gallons'], 50.0)
        self.assertEqual(inv['departure_fuel_valuation_usd'], 175.00)
        self.assertEqual(inv['purchased_fuel_burned_gallons'], 18.50)
        self.assertEqual(inv['total_fuel_remaining_at_destination_gallons'], 0.01)
        self.assertEqual(inv['destination_inventory_valuation_usd'], 0.03)

        # Conservation holds:
        self.assertEqual(inv['total_input_valuation_usd'], 227.39)
        self.assertEqual(inv['total_output_valuation_usd'], 227.39)
        self.assertTrue(inv['is_inventory_conserved'])


if __name__ == '__main__':
    unittest.main()
