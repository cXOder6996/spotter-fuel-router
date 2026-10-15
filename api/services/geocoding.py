import os
import re
import csv
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Bounding box for USA (Continental + AK + HI)
US_BOUNDS = {
    'lat_min': 18.0,
    'lat_max': 72.0,
    'lon_min': -179.0,
    'lon_max': -65.0,
}

# In-memory caches
_LOCAL_CITIES_CACHE = None
_GEOCODE_CACHE = {}


class GeocodingError(Exception):
    """Raised when location cannot be geocoded or resolved."""
    pass


class LocationOutsideUSAError(GeocodingError):
    """Raised when location coordinates fall outside the United States."""
    pass


def _load_local_cities():
    global _LOCAL_CITIES_CACHE
    if _LOCAL_CITIES_CACHE is not None:
        return _LOCAL_CITIES_CACHE

    cities_path = os.path.join(settings.BASE_DIR, 'data', 'us_cities.csv')
    cache = {}
    if os.path.exists(cities_path):
        try:
            with open(cities_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for r in reader:
                    city = r['CITY'].strip().upper()
                    state = r['STATE_CODE'].strip().upper()
                    cache[(city, state)] = (float(r['LATITUDE']), float(r['LONGITUDE']))
        except Exception as e:
            logger.warning(f"Could not load local cities database: {e}")

    _LOCAL_CITIES_CACHE = cache
    return _LOCAL_CITIES_CACHE


def is_within_usa(lat: float, lon: float) -> bool:
    """Checks whether coordinates lie within US territory bounds."""
    return (
        US_BOUNDS['lat_min'] <= lat <= US_BOUNDS['lat_max'] and
        US_BOUNDS['lon_min'] <= lon <= US_BOUNDS['lon_max']
    )


def parse_raw_coordinates(loc_str: str):
    """
    Attempts to parse a comma-separated coordinate pair like '32.7767, -96.7970'.
    Returns (lat, lon) or None.
    """
    match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', loc_str)
    if match:
        lat = float(match.group(1))
        lon = float(match.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


def geocode_location(location: str):
    """
    Resolves a location string to (latitude, longitude, display_name).
    Tries:
      1. Coordinate parsing (0 API calls)
      2. Local US cities database (0 API calls)
      3. External Nominatim geocoder (1 API call with caching)
    """
    clean_loc = location.strip()
    if not clean_loc:
        raise GeocodingError("Location string cannot be empty.")

    # 1. Coordinate input check
    coords = parse_raw_coordinates(clean_loc)
    if coords:
        lat, lon = coords
        if not is_within_usa(lat, lon):
            raise LocationOutsideUSAError(
                f"Coordinates ({lat}, {lon}) are outside the United States."
            )
        return lat, lon, f"{lat:.4f}, {lon:.4f}"

    # Cache check
    norm_key = clean_loc.upper()
    if norm_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[norm_key]

    # 2. Local City/State lookup check
    # Supports 'City, State' or 'City, State, USA'
    parts = [p.strip().upper() for p in clean_loc.split(',')]
    local_cities = _load_local_cities()

    if len(parts) >= 2:
        city_candidate = parts[0]
        state_candidate = parts[1].split()[0] if parts[1] else ''
        if (city_candidate, state_candidate) in local_cities:
            lat, lon = local_cities[(city_candidate, state_candidate)]
            res = (lat, lon, f"{parts[0].title()}, {state_candidate}")
            _GEOCODE_CACHE[norm_key] = res
            return res

    # 3. External Nominatim lookup fallback
    url = "https://nominatim.openstreetmap.org/search"
    headers = {
        'User-Agent': getattr(settings, 'GEOCODING_USER_AGENT', 'SpotterFuelRouter/1.0')
    }
    params = {
        'q': clean_loc,
        'countrycodes': 'us',
        'format': 'json',
        'limit': 1,
    }
    timeout = getattr(settings, 'GEOCODING_TIMEOUT_SECONDS', 5)

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise GeocodingError(
                f"Geocoding service returned HTTP status {resp.status_code}."
            )
        data = resp.json()
        if not data:
            raise GeocodingError(f"Could not find geographic location for '{clean_loc}'.")

        item = data[0]
        lat = float(item['lat'])
        lon = float(item['lon'])
        display_name = item.get('display_name', clean_loc)

        if not is_within_usa(lat, lon):
            raise LocationOutsideUSAError(
                f"Location '{clean_loc}' resolved to ({lat}, {lon}) which is outside the USA."
            )

        result = (lat, lon, display_name)
        _GEOCODE_CACHE[norm_key] = result
        return result

    except requests.exceptions.Timeout:
        raise GeocodingError(f"Geocoding service timed out while resolving '{clean_loc}'.")
    except requests.exceptions.RequestException as e:
        raise GeocodingError(f"Network error during geocoding: {str(e)}")
