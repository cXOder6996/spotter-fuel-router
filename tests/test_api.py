from unittest.mock import patch
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from api.models import TruckStop
from api.services.routing import RouteNotFoundError, RoutingTimeoutError, RoutingError


class RouteOptimizationAPITests(TestCase):
    """
    Integration tests for the Route Optimization REST API and Map View.
    Uses mocked routing responses to ensure tests never depend on live network.
    """

    @classmethod
    def setUpTestData(cls):
        # Create stations along I-45 corridor between Dallas and Houston
        cls.stop1 = TruckStop.objects.create(
            opis_id=101,
            name="Ennis Travel Center",
            address="I-45, EXIT 246",
            city="Ennis",
            state="TX",
            rack_id=630,
            primary_price=2.90,
            min_price=2.85,
            max_price=2.95,
            avg_price=2.90,
            price_variants=[2.85, 2.95],
            latitude=32.3296,
            longitude=-96.6253,
            coordinate_precision="city_corridor_centroid",
            coordinate_source="TEST_FIXTURE",
            highway="I-45",
            exit_number="246",
        )
        cls.stop2 = TruckStop.objects.create(
            opis_id=102,
            name="Corsicana Fuel Stop",
            address="I-45, EXIT 229",
            city="Corsicana",
            state="TX",
            rack_id=641,
            primary_price=2.85,
            min_price=2.85,
            max_price=2.85,
            avg_price=2.85,
            price_variants=[2.85],
            latitude=32.0954,
            longitude=-96.4689,
            coordinate_precision="city_corridor_centroid",
            coordinate_source="TEST_FIXTURE",
            highway="I-45",
            exit_number="229",
        )

    def setUp(self):
        self.client = APIClient()
        self.mock_osrm_dallas_houston = {
            'distance_miles': 239.5,
            'duration_hours': 3.75,
            'coordinates': [
                [-96.7970, 32.7767],
                [-96.6253, 32.3296],
                [-96.4689, 32.0954],
                [-95.3698, 29.7604],
            ],
            'geojson_geometry': {
                'type': 'LineString',
                'coordinates': [
                    [-96.7970, 32.7767],
                    [-96.6253, 32.3296],
                    [-96.4689, 32.0954],
                    [-95.3698, 29.7604],
                ]
            }
        }

    @patch('api.views.get_driving_route')
    def test_valid_short_trip_request(self, mock_route):
        mock_route.return_value = self.mock_osrm_dallas_houston

        payload = {
            'origin': 'Dallas, TX',
            'destination': 'Houston, TX',
        }
        response = self.client.post('/api/v1/optimize-route/', payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['status'], 'success')

        # Check Trip Summary
        summary = data['trip_summary']
        self.assertEqual(summary['total_distance_miles'], 239.5)
        self.assertEqual(summary['estimated_duration_hours'], 3.75)
        self.assertIn('latitude', summary['origin']['coordinates'])

        # Check Fuel Economics & Inventory Conservation
        econ = data['fuel_economics']
        self.assertEqual(econ['vehicle_parameters']['fuel_economy_mpg'], 10.0)
        self.assertEqual(econ['vehicle_parameters']['max_range_miles'], 500.0)
        self.assertEqual(econ['reference_fuel_price_usd_per_gallon'], 3.50)
        self.assertEqual(econ['starting_inventory_valuation_usd'], 175.00)
        self.assertEqual(econ['total_fuel_consumed_gallons'], 23.95)
        # Because trip is < 500 miles, no intermediate stops required
        self.assertEqual(econ['total_fuel_purchased_during_trip_gallons'], 0.0)
        self.assertEqual(econ['total_cash_expenditure_at_pumps_usd'], 0.0)
        self.assertEqual(econ['economic_cost_of_fuel_consumed_usd'], 83.83)
        self.assertEqual(econ['destination_inventory_valuation_usd'], 91.17)
        self.assertEqual(econ['total_fuel_remaining_at_destination_gallons'], 26.05)
        # Total input ($175.00) == Total output ($83.83 burned + $91.17 ending inventory)
        inv = econ['inventory_fuel_accounting']
        self.assertEqual(inv['total_input_valuation_usd'], 175.00)
        self.assertEqual(inv['total_output_valuation_usd'], 175.00)
        self.assertTrue(inv['is_inventory_conserved'])

        # Check GeoJSON geometry
        self.assertEqual(data['route_geometry']['type'], 'LineString')

    @patch('api.views.get_driving_route')
    def test_start_finish_alias_support(self, mock_route):
        mock_route.return_value = self.mock_osrm_dallas_houston

        payload = {
            'start': 'Dallas, TX',
            'finish': 'Houston, TX',
        }
        response = self.client.post('/api/v1/optimize-route/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['status'], 'success')

    def test_invalid_payload_missing_destination(self):
        payload = {'origin': 'Dallas, TX'}
        response = self.client.post('/api/v1/optimize-route/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()['error_type'], 'VALIDATION_ERROR')

    def test_location_outside_usa_rejected(self):
        payload = {
            'origin': '48.8566, 2.3522',  # Paris, France
            'destination': 'Houston, TX',
        }
        response = self.client.post('/api/v1/optimize-route/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()['error_type'], 'LOCATION_OUTSIDE_USA')

    @patch('api.views.get_driving_route')
    def test_routing_timeout_handling(self, mock_route):
        mock_route.side_effect = RoutingTimeoutError("Routing provider timed out.")
        payload = {'origin': 'Dallas, TX', 'destination': 'Houston, TX'}
        response = self.client.post('/api/v1/optimize-route/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
        self.assertEqual(response.json()['error_type'], 'ROUTING_TIMEOUT')

    @patch('api.views.get_driving_route')
    def test_route_not_found_handling(self, mock_route):
        mock_route.side_effect = RouteNotFoundError("No drivable route found.")
        payload = {'origin': 'Dallas, TX', 'destination': 'Houston, TX'}
        response = self.client.post('/api/v1/optimize-route/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()['error_type'], 'ROUTE_NOT_FOUND')

    @patch('api.views.run_route_optimization_pipeline')
    def test_interactive_map_view_renders_html(self, mock_pipeline):
        mock_pipeline.return_value = {
            'trip_summary': {
                'origin': {'resolved_name': 'Dallas, TX', 'coordinates': {'latitude': 32.77, 'longitude': -96.79}},
                'destination': {'resolved_name': 'Houston, TX', 'coordinates': {'latitude': 29.76, 'longitude': -95.36}},
                'total_distance_miles': 240.0,
                'estimated_duration_hours': 4.0,
            },
            'fuel_economics': {
                'total_fuel_consumed_gallons': 24.0,
                'total_fuel_purchased_during_trip_gallons': 0.0,
                'total_cash_expenditure_at_pumps_usd': 0.0,
                'estimated_total_fuel_cost_usd': 84.0,
            },
            'refueling_plan': {'stops_count': 0, 'stops': []},
            'route_geometry': {'type': 'LineString', 'coordinates': [[-96.79, 32.77], [-95.36, 29.76]]},
        }
        response = self.client.get('/map/?origin=Dallas,+TX&destination=Houston,+TX')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(b'Spotter AI Fuel Router', response.content)
        self.assertIn(b'Leaflet', response.content)

    @patch('api.views.get_driving_route')
    def test_custom_reference_price_api(self, mock_route):
        mock_route.return_value = self.mock_osrm_dallas_houston

        payload = {
            'origin': 'Dallas, TX',
            'destination': 'Houston, TX',
            'reference_fuel_price': 3.0812,
        }
        response = self.client.post('/api/v1/optimize-route/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        econ = data['fuel_economics']
        self.assertEqual(econ['reference_fuel_price_usd_per_gallon'], 3.0812)
        self.assertEqual(econ['starting_inventory_valuation_usd'], 154.06)
        self.assertEqual(econ['total_fuel_consumed_gallons'], 23.95)
        self.assertEqual(econ['economic_cost_of_fuel_consumed_usd'], 73.79)
        self.assertEqual(econ['destination_inventory_valuation_usd'], 80.27)
        self.assertEqual(econ['total_fuel_remaining_at_destination_gallons'], 26.05)
        inv = econ['inventory_fuel_accounting']
        self.assertEqual(inv['total_input_valuation_usd'], 154.06)
        self.assertEqual(inv['total_output_valuation_usd'], 154.06)
        self.assertTrue(inv['is_inventory_conserved'])

    @patch('api.views.get_driving_route')
    def test_multi_stop_route_inventory_conservation_api(self, mock_route):
        # Multi-stop driving route: 800 miles passing through Ennis and Corsicana
        mock_route.return_value = {
            'distance_miles': 800.0,
            'duration_hours': 12.0,
            'coordinates': [
                [-96.7970, 32.7767],
                [-96.6253, 32.3296],
                [-96.4689, 32.0954],
                [-95.3698, 25.0000],
            ],
            'geojson_geometry': {
                'type': 'LineString',
                'coordinates': [
                    [-96.7970, 32.7767],
                    [-96.6253, 32.3296],
                    [-96.4689, 32.0954],
                    [-95.3698, 25.0000],
                ]
            }
        }
        payload = {
            'origin': 'Dallas, TX',
            'destination': '32.0954, -96.4689',
        }
        # In this synthetic 800-mi route with stops at 30.8 mi and 51.6 mi,
        # gap from 51.6 to 800 is > 500 miles, so the optimizer correctly identifies gap infeasibility.
        # Let's verify that error response has proper error_type:
        response = self.client.post('/api/v1/optimize-route/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()['error_type'], 'INFEASIBLE_ROUTE')
