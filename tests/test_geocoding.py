import unittest
from unittest.mock import patch, MagicMock
from api.services.geocoding import (
    geocode_location,
    parse_raw_coordinates,
    is_within_usa,
    GeocodingError,
    LocationOutsideUSAError,
)


class GeocodingServiceTests(unittest.TestCase):
    """
    Unit tests for location resolution and US boundary validation.
    """

    def test_parse_valid_coordinates(self):
        coords = parse_raw_coordinates("32.7767, -96.7970")
        self.assertIsNotNone(coords)
        self.assertAlmostEqual(coords[0], 32.7767)
        self.assertAlmostEqual(coords[1], -96.7970)

    def test_us_boundary_detection(self):
        # Dallas, TX -> inside US
        self.assertTrue(is_within_usa(32.7767, -96.7970))
        # Paris, France -> outside US
        self.assertFalse(is_within_usa(48.8566, 2.3522))
        # Tokyo, Japan -> outside US
        self.assertFalse(is_within_usa(35.6762, 139.6503))

    def test_coordinate_outside_usa_raises_error(self):
        with self.assertRaises(LocationOutsideUSAError):
            geocode_location("48.8566, 2.3522")

    def test_empty_string_raises_error(self):
        with self.assertRaises(GeocodingError):
            geocode_location("   ")

    def test_local_city_cache_resolution(self):
        # Dallas, TX should resolve from local cache without network call
        lat, lon, name = geocode_location("Dallas, TX")
        self.assertTrue(is_within_usa(lat, lon))
        self.assertIn("Dallas", name)

    @patch('requests.get')
    def test_nominatim_fallback_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {'lat': '39.7392', 'lon': '-104.9903', 'display_name': 'Denver, Colorado, USA'}
        ]
        mock_get.return_value = mock_resp

        # Force query that triggers external lookup
        lat, lon, name = geocode_location("Unknown Town Nowhere, CO")
        self.assertAlmostEqual(lat, 39.7392)
        self.assertAlmostEqual(lon, -104.9903)
        self.assertEqual(name, 'Denver, Colorado, USA')

    @patch('requests.get')
    def test_nominatim_empty_results_raises_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        with self.assertRaises(GeocodingError) as ctx:
            geocode_location("CompletelyFakePlaceThatDoesNotExist123456")
        self.assertIn("Could not find geographic location", str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
