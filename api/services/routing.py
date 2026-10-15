import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# In-memory routing cache to eliminate duplicate external calls
_ROUTING_CACHE = {}


class RoutingError(Exception):
    """Base exception for routing failures."""
    pass


class RouteNotFoundError(RoutingError):
    """Raised when no drivable route exists between locations."""
    pass


class RoutingTimeoutError(RoutingError):
    """Raised when routing service times out."""
    pass


def get_driving_route(start_lat: float, start_lon: float, dest_lat: float, dest_lon: float):
    """
    Fetches driving route from OSRM in a single API call.
    Returns dictionary with:
      - distance_miles (float)
      - duration_hours (float)
      - coordinates (list of [lon, lat] floats)
      - geojson_geometry (dict)
    """
    # Round coordinates for stable caching (4 decimal places ~ 11 meters)
    cache_key = (round(start_lat, 4), round(start_lon, 4), round(dest_lat, 4), round(dest_lon, 4))
    if cache_key in _ROUTING_CACHE:
        logger.debug(f"Routing cache hit for {cache_key}")
        return _ROUTING_CACHE[cache_key]

    base_url = getattr(settings, 'OSRM_BASE_URL', 'https://router.project-osrm.org')
    # OSRM coordinate order is {lon},{lat}
    url = f"{base_url.rstrip('/')}/route/v1/driving/{start_lon},{start_lat};{dest_lon},{dest_lat}"
    params = {
        'overview': 'full',
        'geometries': 'geojson',
        'steps': 'false',
    }
    headers = {
        'User-Agent': getattr(settings, 'GEOCODING_USER_AGENT', 'SpotterFuelRouter/1.0')
    }
    timeout = getattr(settings, 'ROUTING_TIMEOUT_SECONDS', 10)

    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        raise RoutingTimeoutError("Routing provider timed out while calculating driving route.")
    except requests.exceptions.RequestException as e:
        raise RoutingError(f"Network error communicating with routing provider: {str(e)}")

    if response.status_code != 200:
        raise RoutingError(
            f"Routing provider returned HTTP status {response.status_code}: {response.text[:200]}"
        )

    try:
        data = response.json()
    except ValueError:
        raise RoutingError("Routing provider returned malformed non-JSON response.")

    code = data.get('code')
    if code != 'Ok':
        if code == 'NoRoute':
            raise RouteNotFoundError("No drivable route found between the specified locations.")
        raise RoutingError(f"Routing service returned error code '{code}': {data.get('message', '')}")

    routes = data.get('routes', [])
    if not routes:
        raise RouteNotFoundError("No route geometry returned for the specified locations.")

    primary_route = routes[0]
    distance_meters = float(primary_route.get('distance', 0.0))
    duration_seconds = float(primary_route.get('duration', 0.0))
    geometry = primary_route.get('geometry', {})
    coords = geometry.get('coordinates', [])

    if not coords or len(coords) < 2:
        raise RoutingError("Route geometry contains insufficient coordinate points.")

    distance_miles = distance_meters / 1609.344
    duration_hours = duration_seconds / 3600.0

    result = {
        'distance_miles': round(distance_miles, 2),
        'duration_hours': round(duration_hours, 2),
        'coordinates': coords,  # [[lon, lat], ...]
        'geojson_geometry': geometry,
    }

    _ROUTING_CACHE[cache_key] = result
    return result
