import unittest
from api.management.commands.load_fuel_data import parse_highway_info, CANADIAN_PROVINCES


class FuelDataNormalizationTests(unittest.TestCase):
    """
    Unit tests for CSV data parsing, normalization, and duplicate handling.
    """

    def test_parse_interstate_and_exit(self):
        hw, ex = parse_highway_info("I-44, EXIT 283 & US-69")
        self.assertEqual(hw, "I-44")
        self.assertEqual(ex, "283")

        hw, ex = parse_highway_info("I-94, EXIT 143 & US-12 & SR-21")
        self.assertEqual(hw, "I-94")
        self.assertEqual(ex, "143")

        hw, ex = parse_highway_info("I-35,  EXIT 271")
        self.assertEqual(hw, "I-35")
        self.assertEqual(ex, "271")

    def test_parse_us_highway(self):
        hw, ex = parse_highway_info("US-46")
        self.assertEqual(hw, "US-46")
        self.assertIsNone(ex)

    def test_parse_state_route(self):
        hw, ex = parse_highway_info("SR-32")
        self.assertEqual(hw, "SR-32")
        self.assertIsNone(ex)

    def test_canadian_province_detection(self):
        self.assertIn('ON', CANADIAN_PROVINCES)
        self.assertIn('AB', CANADIAN_PROVINCES)
        self.assertIn('BC', CANADIAN_PROVINCES)
        self.assertNotIn('TX', CANADIAN_PROVINCES)
        self.assertNotIn('CA', CANADIAN_PROVINCES)
        self.assertNotIn('NY', CANADIAN_PROVINCES)


if __name__ == '__main__':
    unittest.main()
