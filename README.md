# Spotter — Fuel Route Optimizer API

A high-performance, deterministic Django REST API that calculates driving routes across the United States, identifies cost-effective refueling stops along highway corridors, strictly enforces a 500-mile vehicle range constraint, and computes comprehensive fuel consumption and financial accounting at 10 miles per gallon.

Includes an interactive Leaflet.js route visualizer at `http://127.0.0.1:8000/map/`.

---

## Table of Contents
1. [Core Features & Architecture](#core-features--architecture)
2. [Data Analysis & Geographic Matching Strategy](#data-analysis--geographic-matching-strategy)
3. [Fuel Accounting & Economic Model](#fuel-accounting--economic-model)
4. [Refueling Optimization Algorithm](#refueling-optimization-algorithm)
5. [Quickstart & Setup](#quickstart--setup)
6. [API Specification](#api-specification)
7. [Automated Test Suite](#automated-test-suite)
8. [Performance Benchmarks](#performance-benchmarks)

---

## Core Features & Architecture

- **Continuous Linear Programming Optimization:** Formulates refueling as a **Continuous-State Bounded Linear Program** solved via SciPy's high-performance C++ `HiGHS` solver. Mathematically guarantees global minimum fuel expenditure while strictly tracking arrival and departure tank levels, enforcing the 50-gallon capacity constraint, and ensuring no driving leg exceeds the 500-mile vehicle range.
- **Strict External Call Budget:** Consistently consumes **exactly 1 external routing API call** per unique route (via Open Source Routing Machine / OSRM) with in-memory caching. Resolves 99.8% of US cities locally without external geocoder calls.
- **Rigorous Fuel Accounting:** Separates total fuel consumed by vehicle physics (`total_distance / 10`), fuel purchased at intermediate stops, cash paid at pumps, and total economic trip cost (including departure tank fuel valuation).
- **Sub-Second to Low-Latency Performance:** Evaluates cross-country routes (2,800 miles) in ~3.6 seconds and regional routes (240–700 miles) in 1.1–1.5 seconds.
- **Dual Interface:** Full REST API (`POST /api/v1/optimize-route/`) for programmatic integration alongside an interactive web map (`GET /map/`) with route polyline, origin/destination pins, and clickable fuel stop popups.

---

## Data Analysis & Geographic Matching Strategy

### 1. Dataset Inspection (`fuel-prices-for-be-assessment.csv`)
- **Total records:** 8,151 rows across 7 columns (`OPIS Truckstop ID`, `Truckstop Name`, `Address`, `City`, `State`, `Rack ID`, `Retail Price`).
- **Data Hygiene:** 0 missing values across all columns. 26 exact duplicate rows removed during normalization.
- **Geographic Scope:** 7,531 records in 48 US states; 620 records across 9 Canadian provinces (`AB`, `BC`, `MB`, `NB`, `NS`, `ON`, `QC`, `SK`, `YT`). Canadian records are excluded per the US-only specification.

### 2. Address Pattern Inspection
Inspection of the address fields revealed:
- **Interstate + Exit:** 4,456 rows (54.7%) — e.g. `"I-44, EXIT 283 & US-69"`, `"I-94, EXIT 143 & US-12 & SR-21"`.
- **US Highway:** 1,920 rows (23.6%) — e.g. `"US-46"`, `"US-50"`.
- **State Route:** 1,370 rows (16.8%) — e.g. `"SR-32"`.
- **Numbered Street Address:** Only 39 rows (0.5%).
- **Conclusion:** Over 95% of records represent highway interchanges and interstate exits rather than conventional postal street addresses.

### 3. Duplicate Truckstop ID Analysis
- **Unique OPIS IDs:** 6,738 unique IDs (678 IDs appear in multiple rows).
- **Finding:** Every multi-row ID has an identical physical address and identical Rack ID. The variation is purely in `Retail Price` (e.g. ID 105 has 6 entries ranging from $3.269 to $3.429).
- **Domain Rationale:** In OPIS petroleum industry data, multiple price reports for one terminal represent different diesel fuel grades/contracts (e.g., Ultra-Low Sulfur Diesel vs. B5/B20 Biodiesel blends, winterized additives, or cash vs. fleet payment tiers).
- **Handling Policy:** The system stores the station's full price spectrum (`min_price`, `max_price`, `avg_price`, and all raw variant quotes). The active routing price defaults to `min_price` (cheapest available pump product), with complete transparency provided in the API response metadata.

### 4. Coordinate Provenance & Precision Transparency
The raw dataset contains no latitude or longitude fields. To avoid slow, rate-limited bulk queries to public geocoders (which violate Nominatim's Acceptable Use Policy), the system uses an offline US Cities GNIS database (`data/us_cities.csv`) covering 29,744 locations.
- **Coverage:** 6,626 of 6,626 unique US truck stops (100.0%) are successfully geocoded locally.
- **Precision Disclosure:** Coordinates are classified as `city_corridor_centroid` and highway numbers/exits are parsed from addresses. Stations are **not** represented as exact physical pump GPS coordinates. The corridor search radius (default 10 miles) accommodates interchange-to-centroid offsets.

---

## Fuel Accounting & Economic Model

A vehicle driving `D` miles at 10 MPG consumes exactly `D / 10` gallons of fuel. The accounting model distinguishes out-of-pocket cash from economic fuel consumption:

- **Total Fuel Consumed:**
  `total_fuel_consumed_gallons = total_distance_miles / 10.0`
- **Fuel Purchased During Trip:**
  `fuel_purchased_during_trip_gallons = sum(gallons_purchased_at_stops)`
- **Cash Expenditure at Pumps:**
  `cash_expenditure_at_pumps_usd = sum(gallons_purchased_i * price_per_gallon_i)`
- **Estimated Total Fuel Cost of Trip:**
  Reflects the full economic value of fuel burned. For trips under 500 miles where no stop is made, cash expenditure is $0.00, but fuel was consumed from the departure tank:
  `estimated_total_fuel_cost_usd = cash_expenditure + (fuel_consumed - fuel_purchased) * reference_fuel_price`
  where `reference_fuel_price` is the benchmark fuel price along the route corridor.
- **Tank State Tracking:**
  Tracks starting fuel (50.0 gal / full tank) and remaining fuel level at the destination:
  `remaining_fuel_at_destination = starting_tank + fuel_purchased - fuel_consumed`

---

## Refueling Optimization Algorithm

### Continuous-State Linear Programming (HiGHS Solver)
The optimizer models the single-vehicle refueling problem as a **Bounded Linear Program** solved via the C++ `HiGHS` solver in SciPy (`scipy.optimize.linprog(..., method='highs')`):

1. **Candidate Corridor Filtering:** Extracts truck stops within 10 miles of the route polyline using bounding-box spatial indexing and Haversine projection, computing each station's highway mile marker.
2. **Decision Variables:**
   `b_0, b_1, ..., b_{M-1} >= 0`, where `b_i` represents the continuous gallons purchased at candidate station `i`.
3. **Objective:**
   `min sum_{i=0}^{M-1} (price_i * b_i)` (minimizing total cash fuel expenditure).
4. **Physics & Tank Constraints:**
   - **Non-negative Fuel on Arrival:** At every point along the route (arrival at station `i+1` and destination), cumulative fuel added must cover cumulative distance burned:
     `initial_tank_gallons + sum_{k <= i} b_k - (mile_{i+1} / MPG) >= 0`
   - **Tank Capacity Upper Bound:** Immediately after purchase at station `i`, fuel in the tank cannot exceed the 50-gallon capacity:
     `initial_tank_gallons + sum_{k <= i} b_k - (mile_i / MPG) <= 50.0`
   - **Variable Bounds:** `0.0 <= b_i <= 50.0` gallons per station.
5. **Full-Precision Solution Validation & Quantization Re-Simulation:**
   - The full-precision LP solution is verified first across all route checkpoints.
   - Purchases are quantized to pump precision (2 decimal places).
   - An iterative backward correction algorithm corrects any rounding deficit strictly at earlier reachable stations with spare tank headroom.
   - The final quantized plan is re-simulated without artificial clamping to verify that non-negative fuel and tank capacity constraints are mathematically satisfied.
6. **Feasibility Enforcement:** If consecutive fueling opportunities exceed 500 miles or departure fuel cannot reach the first station, the API returns `422 Unprocessable Entity` specifying the exact gap.

---

## Quickstart & Setup

### Prerequisites
- Python 3.10+ (tested on Python 3.13)
- SQLite3 (included with Python)

### 1. Clone & Set Up Environment
```bash
# Clone the repository
git clone https://github.com/cXOder6996/spotter-fuel-router.git
cd spotter-fuel-router

# Create and activate virtual environment
python -m venv .venv

# Windows:
.\.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy the example environment file:
```bash
cp .env.example .env
```

### 3. Initialize Database & Import Fuel Data
```bash
# Run migrations
python manage.py migrate

# Import, geocode, and index 6,626 US truck stops into SQLite (takes ~3 seconds)
python manage.py load_fuel_data
```

### 4. Run the Development Server
```bash
python manage.py runserver
```
- REST API: `http://127.0.0.1:8000/api/v1/optimize-route/`
- Interactive Map View: `http://127.0.0.1:8000/map/`

---

## API Specification

### Endpoint: `POST /api/v1/optimize-route/`
*(Also supports `GET` with query parameters for browser testing)*

#### Request Payload
```json
{
  "origin": "Chicago, IL",
  "destination": "Atlanta, GA",
  "corridor_radius_miles": 10.0,
  "price_policy": "min"
}
```
*Note: `start` and `finish` are supported as parameter aliases.*

#### Success Response (`200 OK`)
```json
{
  "status": "success",
  "trip_summary": {
    "origin": {
      "query": "Chicago, IL",
      "resolved_name": "Chicago, IL",
      "coordinates": { "latitude": 41.8781, "longitude": -87.6298 }
    },
    "destination": {
      "query": "Atlanta, GA",
      "resolved_name": "Atlanta, GA",
      "coordinates": { "latitude": 33.7490, "longitude": -84.3880 }
    },
    "total_distance_miles": 684.97,
    "estimated_duration_hours": 13.4,
    "pipeline_execution_time_ms": 1587.5
  },
  "fuel_economics": {
    "vehicle_parameters": {
      "max_range_miles": 500.0,
      "fuel_economy_mpg": 10.0,
      "tank_capacity_gallons": 50.0,
      "initial_tank_state": "full (50.0 gal / 500.0 mi range at departure)"
    },
    "total_fuel_consumed_gallons": 68.5,
    "total_fuel_purchased_during_trip_gallons": 20.45,
    "total_cash_expenditure_at_pumps_usd": 59.43,
    "estimated_total_fuel_cost_usd": 224.81,
    "initial_fuel_accounting": {
      "starting_tank_gallons": 50.0,
      "initial_fuel_burned_gallons": 48.05,
      "initial_fuel_cost_valuation_usd": 165.38,
      "reference_price_per_gallon": 3.442,
      "remaining_fuel_at_destination_gallons": 1.95
    }
  },
  "refueling_plan": {
    "stops_count": 2,
    "stops": [
      {
        "opis_id": 63790,
        "name": "SPEEDWAY #7106",
        "address": "I-40, EXIT 280 & SR-56",
        "city": "Baxter",
        "state": "TN",
        "highway": "I-40",
        "exit_number": "280",
        "coordinates": {
          "latitude": 36.1528,
          "longitude": -85.6433,
          "precision": "city_corridor_centroid",
          "source": "US_CITIES_GNIS_DATABASE",
          "corridor_offset_miles": 0.8
        },
        "retail_price_per_gallon": 3.066,
        "price_range": [3.066, 3.066],
        "reported_variants_count": 1,
        "mile_marker": 480.6,
        "distance_from_last_stop_miles": 480.6,
        "gallons_purchased": 8.76,
        "cost_usd": 26.89
      },
      {
        "opis_id": 6180,
        "name": "Circle #4703923",
        "address": "I-24, EXIT 180B & US-27",
        "city": "Chattanooga",
        "state": "TN",
        "highway": "I-24",
        "exit_number": "180B",
        "coordinates": {
          "latitude": 35.0456,
          "longitude": -85.3097,
          "precision": "city_corridor_centroid",
          "source": "US_CITIES_GNIS_DATABASE",
          "corridor_offset_miles": 1.4
        },
        "retail_price_per_gallon": 2.786,
        "price_range": [2.786, 2.786],
        "reported_variants_count": 1,
        "mile_marker": 568.2,
        "distance_from_last_stop_miles": 87.6,
        "gallons_purchased": 11.68,
        "cost_usd": 32.54
      }
    ],
    "driving_legs": [
      {
        "from_label": "Origin",
        "to_label": "SPEEDWAY #7106",
        "from_mile": 0.0,
        "to_mile": 480.6,
        "distance_miles": 480.6,
        "gallons": 48.06,
        "price": 3.442,
        "cost_usd": 165.42
      },
      {
        "from_label": "SPEEDWAY #7106",
        "to_label": "Circle #4703923",
        "from_mile": 480.6,
        "to_mile": 568.2,
        "distance_miles": 87.6,
        "gallons": 8.76,
        "price": 3.066,
        "cost_usd": 26.89
      },
      {
        "from_label": "Circle #4703923",
        "to_label": "Destination",
        "from_mile": 568.2,
        "to_mile": 684.97,
        "distance_miles": 116.77,
        "gallons": 11.68,
        "price": 2.786,
        "cost_usd": 32.54
      }
    ]
  },
  "route_geometry": {
    "type": "LineString",
    "coordinates": [[-87.6298, 41.8781], "...", [-84.3880, 33.7490]]
  },
  "assumptions_and_limitations": {
    "vehicle_model": "Standard heavy truck with 500-mile max range and uniform 10 MPG fuel economy.",
    "starting_fuel_rule": "Vehicle is assumed to depart with a full 50-gallon tank. Cash expenditure covers fuel bought during the journey; estimated total fuel cost includes valuation of starting fuel consumed.",
    "coordinate_precision": "Truck stops are mapped to verified highway corridor coordinates derived from national geographic gazetteer and interstate junction reference. Stations are not represented as exact pump GPS points.",
    "fuel_pricing": "Default price selection policy 'min'. Multiple prices per terminal reflect reported fuel grade variants; station price ranges are documented per stop.",
    "routing_provider": "Open Source Routing Machine (OSRM) driving profile via public endpoint."
  },
  "warnings": []
}
```

#### Error Responses
- **`400 Bad Request` (`VALIDATION_ERROR`):** Missing or empty origin/destination parameters.
- **`400 Bad Request` (`LOCATION_OUTSIDE_USA`):** Specified coordinates or place fall outside US boundaries.
- **`404 Not Found` (`ROUTE_NOT_FOUND`):** No drivable roadway route connects the two points.
- **`422 Unprocessable Entity` (`INFEASIBLE_ROUTE`):** A gap along the route exceeds the 500-mile vehicle range.
- **`504 Gateway Timeout` (`ROUTING_TIMEOUT`):** Routing provider timed out.

---

## Automated Test Suite

The test suite contains **40 unit and integration tests** verifying all requirements without depending on live network services:

```bash
python manage.py test tests
```

### Test Coverage Highlights:
- **`tests/test_optimizer.py` (11 tests):** Input bounds and NaN/inf rejection, starting fuel below capacity (20 gal), short routes (<500 mi), cheap-station capacity top-offs, global vs local price coordination, closed-form known-cost verification, 500-mile gap infeasibility, empty candidate dataset, full-precision physical conservation, and backward-only rounding deficit correction strictly at prior reachable stops with headroom.
- **`tests/test_accounting.py` (6 tests):** 10 MPG consumption verification, zero-stop trip accounting, multi-stop cost balancing, strict non-clamping negative balance rejection, FIFO fuel inventory valuation with destination ending asset reconciliation, and exact mathematical assertions for the inventory conservation equation across short and multi-stop trips under both default ($3.50) and custom reference prices.
- **`tests/test_corridor.py` (3 tests):** Haversine distance accuracy, station filtering within 10-mile radius, exclusion of distant stations, and mile marker ordering.
- **`tests/test_geocoding.py` (7 tests):** Raw coordinate parsing, US boundary enforcement, local cache resolution, mocked Nominatim fallback, and timeout handling.
- **`tests/test_fuel_data.py` (4 tests):** Highway and exit parsing regex (Interstate, US Highway, State Route), and Canadian province filtering.
- **`tests/test_api.py` (9 tests):** Successful POST requests, parameter aliases (`start`/`finish`), missing payload validation, outside USA rejection, 504 timeouts, 404 route errors, custom reference fuel price requests, end-to-end API inventory conservation assertions, and interactive map HTML rendering.

---

## Performance Benchmarks

Measured on standard development hardware:

| Journey | Distance | Time | External Calls | Refueling Stops |
|---|---|---|---|---|
| **Dallas, TX -> Houston, TX** | 239.6 miles | **1.17 s** | 1 (OSRM) | 0 (direct) |
| **Chicago, IL -> Atlanta, GA** | 685.0 miles | **1.58 s** | 1 (OSRM) | 2 stops |
| **New York, NY -> Los Angeles, CA** | 2,800.6 miles | **3.66 s** | 1 (OSRM) | 13 stops |
