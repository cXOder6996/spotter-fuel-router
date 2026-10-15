import math
from django.test import TestCase
from api.models import TruckStop
from api.services.corridor import haversine_miles, find_corridor_truck_stops


class CorridorFilterTests(TestCase):
    """
    Unit tests for corridor projection and spatial filtering.
    """

    def setUp(self):
        # Create test stations along an east-west trajectory along latitude 35.0
        # lon -100 to -90 (~560 miles)
        TruckStop.objects.create(
            opis_id=1,
            name="Near Station Mile 100",
            address="I-40 EXIT 10",
            city="Amarillo",
            state="TX",
            rack_id=100,
            primary_price=3.00,
            min_price=3.00,
            max_price=3.00,
            avg_price=3.00,
            price_variants=[3.00],
            latitude=35.02,  # ~1.4 miles north of line 35.0
            longitude=-98.0,
            coordinate_precision="city_corridor_centroid",
            coordinate_source="TEST",
            highway="I-40",
            exit_number="10",
        )
        TruckStop.objects.create(
            opis_id=2,
            name="Far Off Station",
            address="Far Away",
            city="FarCity",
            state="TX",
            rack_id=100,
            primary_price=2.50,
            min_price=2.50,
            max_price=2.50,
            avg_price=2.50,
            price_variants=[2.50],
            latitude=35.80,  # ~55 miles north of line 35.0
            longitude=-98.0,
            coordinate_precision="city_corridor_centroid",
            coordinate_source="TEST",
        )

    def test_haversine_accuracy(self):
        # 1 degree latitude is approximately 69.0 miles
        dist = haversine_miles(35.0, -98.0, 36.0, -98.0)
        self.assertAlmostEqual(dist, 69.0, delta=1.0)

    def test_corridor_filtering_includes_near_and_excludes_far(self):
        # Route along latitude 35.0 from lon -100 to -96
        # [[lon, lat], ...]
        route_coords = [
            [-100.0, 35.0],
            [-98.0, 35.0],
            [-96.0, 35.0],
        ]
        results = find_corridor_truck_stops(route_coords, corridor_radius_miles=10.0)

        opis_ids = [r['station'].opis_id for r in results]
        self.assertIn(1, opis_ids, "Near station should be included within 10-mile radius")
        self.assertNotIn(2, opis_ids, "Far station (>50 miles off route) should be excluded")

    def test_empty_route_returns_empty(self):
        self.assertEqual(find_corridor_truck_stops([]), [])
        self.assertEqual(find_corridor_truck_stops([[-98.0, 35.0]]), [])
