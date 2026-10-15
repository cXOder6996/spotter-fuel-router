import os
import csv
import re
import statistics
from collections import defaultdict
from django.core.management.base import BaseCommand
from django.db import transaction
from django.conf import settings
from api.models import TruckStop

CANADIAN_PROVINCES = {'AB', 'BC', 'MB', 'NB', 'NS', 'ON', 'QC', 'SK', 'YT', 'NT', 'NU', 'PE', 'NL'}

CITY_ALIASES = {
    ('BROOKPARK', 'OH'): ('BROOK PARK', 'OH'),
    ('UNIVERSITY PARK', 'IL'): (41.4442, -87.6853),
    ('PORT WENTWORTH', 'GA'): (32.1494, -81.1632),
    ('EVERGREEN', 'AL'): (31.4338, -86.9547),
    ('ELIZABETHPORT', 'NJ'): (40.6640, -74.2107),
    ('HENRICO', 'VA'): (37.5385, -77.3486),
}


def parse_highway_info(addr):
    """
    Extracts interstate/US highway and exit numbers from address strings.
    Example: 'I-44, EXIT 283 & US-69' -> ('I-44', '283')
    """
    hw = None
    ex = None
    hw_match = re.search(r'\b(I-\d+|US-\d+|SR-\d+|RT-\d+|HWY\s*\d+)\b', addr, re.I)
    if hw_match:
        hw = hw_match.group(1).upper().replace(' ', '')
    ex_match = re.search(r'EXIT\s*([0-9]+[A-Z]?)', addr, re.I)
    if ex_match:
        ex = ex_match.group(1).upper()
    return hw, ex


class Command(BaseCommand):
    help = 'Imports fuel price data from CSV, resolves coordinates, and loads into database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv',
            type=str,
            default=os.path.join(settings.BASE_DIR, 'fuel-prices-for-be-assessment.csv'),
            help='Path to fuel prices CSV file'
        )
        parser.add_argument(
            '--cities',
            type=str,
            default=os.path.join(settings.BASE_DIR, 'data', 'us_cities.csv'),
            help='Path to US cities reference CSV'
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing truck stop records before importing'
        )

    def handle(self, *args, **options):
        csv_path = options['csv']
        cities_path = options['cities']

        if not os.path.exists(csv_path):
            self.stderr.write(self.style.ERROR(f"Fuel CSV not found at {csv_path}"))
            return

        if not os.path.exists(cities_path):
            self.stderr.write(self.style.ERROR(f"Cities database not found at {cities_path}"))
            return

        # 1. Load US cities coordinate mapping
        self.stdout.write(f"Loading reference cities from {cities_path}...")
        city_coords = {}
        with open(cities_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                key = (r['CITY'].strip().upper(), r['STATE_CODE'].strip().upper())
                city_coords[key] = (float(r['LATITUDE']), float(r['LONGITUDE']))

        # Apply specific geographic aliases
        for alias_key, target in CITY_ALIASES.items():
            if isinstance(target, tuple) and isinstance(target[0], str):
                if target in city_coords:
                    city_coords[alias_key] = city_coords[target]
            elif isinstance(target, tuple):
                city_coords[alias_key] = target

        self.stdout.write(f"Loaded {len(city_coords):,} city reference coordinates.")

        # 2. Read and group fuel prices CSV
        self.stdout.write(f"Reading fuel prices from {csv_path}...")
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader)
            raw_rows = list(reader)

        total_rows = len(raw_rows)
        canadian_skipped = 0
        grouped_stations = defaultdict(list)

        for r in raw_rows:
            if len(r) < 7:
                continue
            state = r[4].strip().upper()
            if state in CANADIAN_PROVINCES:
                canadian_skipped += 1
                continue

            opis_id = int(r[0].strip())
            name = r[1].strip()
            address = r[2].strip()
            city = r[3].strip()
            rack_id = int(r[5].strip())
            try:
                price = float(r[6].strip())
            except ValueError:
                continue

            grouped_stations[opis_id].append({
                'name': name,
                'address': address,
                'city': city,
                'state': state,
                'rack_id': rack_id,
                'price': price,
            })

        self.stdout.write(
            f"Processed {total_rows:,} raw rows ({canadian_skipped:,} Canadian skipped). "
            f"Grouped into {len(grouped_stations):,} unique US truck stops."
        )

        # 3. Build model instances
        station_objects = []
        unmatched_count = 0

        for opis_id, entries in grouped_stations.items():
            first = entries[0]
            city_upper = first['city'].upper()
            state_upper = first['state'].upper()

            coords = city_coords.get((city_upper, state_upper))
            if not coords:
                unmatched_count += 1
                continue

            lat, lon = coords
            prices = [e['price'] for e in entries]
            min_p = min(prices)
            max_p = max(prices)
            avg_p = statistics.mean(prices)

            # Determine primary price based on policy
            policy = getattr(settings, 'DEFAULT_FUEL_PRICE_POLICY', 'min')
            if policy == 'avg':
                primary_p = avg_p
            elif policy == 'max':
                primary_p = max_p
            else:
                primary_p = min_p

            highway, exit_no = parse_highway_info(first['address'])

            station_objects.append(TruckStop(
                opis_id=opis_id,
                name=first['name'],
                address=first['address'],
                city=first['city'],
                state=first['state'],
                rack_id=first['rack_id'],
                primary_price=round(primary_p, 4),
                min_price=round(min_p, 4),
                max_price=round(max_p, 4),
                avg_price=round(avg_p, 4),
                price_variants=prices,
                latitude=lat,
                longitude=lon,
                coordinate_precision='city_corridor_centroid',
                coordinate_source='US_CITIES_GNIS_DATABASE',
                highway=highway,
                exit_number=exit_no,
            ))

        # 4. Insert into database
        with transaction.atomic():
            if options['clear']:
                TruckStop.objects.all().delete()
            else:
                TruckStop.objects.all().delete()  # idempotent reload
            TruckStop.objects.bulk_create(station_objects, batch_size=1000)

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully loaded {len(station_objects):,} US truck stops into SQLite database! "
                f"(Unmatched: {unmatched_count})"
            )
        )
