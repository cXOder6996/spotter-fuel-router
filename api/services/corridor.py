import math
from typing import List, Dict, Any
from django.conf import settings
from api.models import TruckStop


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great-circle distance in miles between two coordinates."""
    r_miles = 3958.7613
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r_miles * c


def find_corridor_truck_stops(
    route_coordinates: List[List[float]],
    corridor_radius_miles: float = None
) -> List[Dict[str, Any]]:
    """
    Finds and projects truck stops within the driving corridor along the route.

    Args:
        route_coordinates: List of [lon, lat] coordinate pairs along the route polyline.
        corridor_radius_miles: Max distance (in miles) from route polyline.

    Returns:
        List of dicts ordered by mile_marker ascending:
        [
            {
                'station': TruckStop instance,
                'mile_marker': float (distance from route start in miles),
                'corridor_distance_miles': float (perpendicular offset from route),
            },
            ...
        ]
    """
    if not route_coordinates or len(route_coordinates) < 2:
        return []

    if corridor_radius_miles is None:
        corridor_radius_miles = getattr(settings, 'ROUTE_CORRIDOR_RADIUS_MILES', 10.0)

    # 1. Compute cumulative route distance along polyline vertices
    # polyline is [[lon, lat], ...]
    route_points = []  # [(lat, lon, cumulative_mile), ...]
    cum_dist = 0.0
    first_lon, first_lat = route_coordinates[0]
    route_points.append((first_lat, first_lon, 0.0))

    for i in range(1, len(route_coordinates)):
        prev_lon, prev_lat = route_coordinates[i - 1]
        curr_lon, curr_lat = route_coordinates[i]
        seg_dist = haversine_miles(prev_lat, prev_lon, curr_lat, curr_lon)
        cum_dist += seg_dist
        route_points.append((curr_lat, curr_lon, cum_dist))

    # 2. Coarse bounding box filter
    lats = [p[0] for p in route_points]
    lons = [p[1] for p in route_points]
    # buffer in degrees: 1 deg lat ~ 69 miles; add generous buffer
    lat_buffer = (corridor_radius_miles / 69.0) + 0.1
    lon_buffer = (corridor_radius_miles / (69.0 * math.cos(math.radians(sum(lats) / len(lats))))) + 0.1

    candidates = TruckStop.objects.filter(
        latitude__gte=min(lats) - lat_buffer,
        latitude__lte=max(lats) + lat_buffer,
        longitude__gte=min(lons) - lon_buffer,
        longitude__lte=max(lons) + lon_buffer
    )

    if not candidates.exists():
        return []

    # 3. Fine projection of each candidate station onto route polyline
    # Subsample route points if polyline is very dense (>1000 points) for responsiveness
    sample_step = max(1, len(route_points) // 1000)
    sampled_route = route_points[::sample_step]
    if sampled_route[-1] != route_points[-1]:
        sampled_route.append(route_points[-1])

    matched_stops = []
    seen_opis_ids = set()

    for station in candidates:
        if station.opis_id in seen_opis_ids:
            continue

        best_distance = float('inf')
        best_mile_marker = 0.0

        for r_lat, r_lon, r_mile in sampled_route:
            d = haversine_miles(station.latitude, station.longitude, r_lat, r_lon)
            if d < best_distance:
                best_distance = d
                best_mile_marker = r_mile

        if best_distance <= corridor_radius_miles:
            seen_opis_ids.add(station.opis_id)
            matched_stops.append({
                'station': station,
                'mile_marker': round(best_mile_marker, 2),
                'corridor_distance_miles': round(best_distance, 2),
            })

    # Sort candidates strictly by their mile marker along the driving route
    matched_stops.sort(key=lambda item: (item['mile_marker'], item['station'].primary_price))
    return matched_stops
