# Spotter AI — Optimal Fuel Routing API

A high-performance, deterministic Django REST API that calculates driving routes across the United States, identifies cost-effective refueling stops along highway corridors, enforces a strict 500-mile vehicle range constraint, and computes comprehensive fuel accounting (at 10 miles per gallon).

Includes an interactive Leaflet.js route visualizer at `http://127.0.0.1:8000/map/`.

---

## Table of Contents
1. [Core Features & Engineering Highlights](#core-features--engineering-highlights)
2. [Data Analysis & Geographic Matching Strategy](#data-analysis--geographic-matching-strategy)
3. [Fuel Accounting & Economic Model](#fuel-accounting--economic-model)
4. [Refueling Optimization Algorithm](#refueling-optimization-algorithm)
5. [Quickstart & Setup](#quickstart--setup)
6. [API Specification](#api-specification)
7. [Automated Test Suite](#automated-test-suite)
8. [Performance Benchmarks](#performance-benchmarks)
9. [5-Minute Loom Walkthrough Script](#5-minute-loom-walkthrough-script)

---

## Core Features & Engineering Highlights

- **Continuous Linear Programming Optimization:** Formulates refueling as a **Continuous-State Bounded Linear Program** solved via SciPy's high-performance C++ `HiGHS` solver. Guarantees global minimum fuel expenditure while strictly tracking arrival and departure tank levels, enforcing the 50-gallon capacity constraint, and ensuring no driving leg exceeds the 500-mile vehicle range.
- **Strict External Call Budget:** Consistently consumes **exactly 1 external routing API call** per unique route (via Open Source Routing Machine / OSRM) with in-memory caching. Resolves 99.8% of US cities locally without external geocoder calls.
- **Rigorous Fuel Accounting:** Separates total fuel consumed by vehicle physics ($D / 10$), fuel purchased at intermediate stops, cash paid at pumps, and total economic trip cost (including departure tank fuel valuation).
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
- **Finding:** Every multi-row ID has an identical physical address and identical Rack ID. The variation is purely in `Retail Price` (e.g. ID 105 has 6 entries ranging from \$3.269 to \$3.429).
- **Domain Rationale:** In OPIS petroleum industry data, multiple price reports for one terminal represent different diesel fuel grades/contracts (e.g., Ultra-Low Sulfur Diesel vs. B5/B20 Biodiesel blends, winterized additives, or cash vs. fleet payment tiers).
- **Handling Policy:** The system stores the station's full price spectrum (`min_price`, `max_price`, `avg_price`, and all raw variant quotes). The active routing price defaults to `min_price` (cheapest available pump product), with complete transparency provided in the API response metadata.

### 4. Coordinate Provenance & Precision Transparency
The raw dataset contains no latitude or longitude fields. To avoid slow, rate-limited bulk queries to public geocoders (which violate Nominatim's Acceptable Use Policy), the system uses an offline US Cities GNIS database (`data/us_cities.csv`) covering 29,744 locations.
- **Coverage:** 6,626 of 6,626 unique US truck stops (100.0%) are successfully geocoded locally.
- **Precision Disclosure:** Coordinates are classified as `city_corridor_centroid` and highway numbers/exits are parsed from addresses. Stations are **not** represented as exact physical pump GPS coordinates. The corridor search radius (default 10 miles) accommodates interchange-to-centroid offsets.

---

## Fuel Accounting & Economic Model

A vehicle driving $D$ miles at 10 MPG consumes exactly $D / 10$ gallons of fuel. The accounting distinguishes out-of-pocket cash from economic fuel consumption and inventory:

1. **Total Fuel Consumed:**
   $$\text{total\_fuel\_consumed\_gallons} = \frac{\text{total\_distance\_miles}}{10}$$
2. **Fuel Purchased During Trip:**
   $$\text{fuel\_purchased\_during\_trip\_gallons} = \sum_{i=1}^k \text{gallons\_purchased}_i$$
3. **Cash Expenditure at Pumps:**
   $$\text{total\_cash\_expenditure\_at\_pumps\_usd} = \sum_{i=1}^k (\text{gallons\_purchased}_i \times \text{price}_i)$$
4. **Destination Remaining Fuel Inventory:**
   $$\text{destination\_remaining\_fuel\_gallons} = \text{starting\_tank} + \text{fuel\_purchased} - \text{fuel\_consumed}$$
   Strictly reconciled without artificial clamping: non-negative fuel and tank capacity constraints are mathematically enforced.
5. **Economic Cost of Fuel Consumed (FIFO Inventory Valuation):**
   Reflects the actual monetary value of fuel burned during this trip:
   - Departure fuel burned (up to starting tank) is valued at corridor benchmark reference price $P_{\text{reference}}$.
   - Remaining fuel burned is drawn sequentially from intermediate pump purchases.
   - Any fuel purchased but remaining in the tank at destination is treated as ending asset inventory, guaranteeing:
   $$\text{Total Input (Departure Valuation + Cash Spent)} = \text{Economic Cost of Burned Fuel} + \text{Ending Inventory Value}$$

---

## Refueling Optimization Algorithm

### Continuous-State Linear Programming (HiGHS Solver)
The optimizer formulates the single-vehicle continuous-tank refueling problem as a **Bounded Linear Program** solved via the high-performance C++ `HiGHS` solver in SciPy:

1. **Candidate Corridor Filtering:** Extracts truck stops within 10 miles of the route polyline using bounding-box spatial indexing and Haversine projection, computing each station's highway mile marker.
2. **Decision Variables:**
   $b_0, b_1, \dots, b_{M-1} \ge 0$, where $b_i$ represents the continuous gallons purchased at candidate station $i$.
3. **Objective:**
   $$\min \sum_{i=0}^{M-1} (\text{price}_i \times b_i)$$
4. **Physics & Tank Constraints:**
   - **Non-negative Fuel Constraint:** At every point along the route (arrival at station $i+1$ and destination), cumulative fuel added must cover cumulative distance burned:
     $$\text{Initial Fuel} + \sum_{k=0}^i b_k - \frac{\text{mile}_{i+1}}{\text{MPG}} \ge 0$$
   - **Tank Capacity Constraint:** Immediately after purchase at station $i$, fuel cannot exceed the 50-gallon tank capacity:
     $$\text{Initial Fuel} + \sum_{k=0}^i b_k - \frac{\text{mile}_i}{\text{MPG}} \le 50.0$$
5. **Full-Precision Validation & Quantization Re-Simulation:**
   The full-precision solution is verified first, quantized to pump precision (2 decimals), and re-simulated without clamping. Any physical deficit or capacity violation exceeds tolerance triggers an immediate `InfeasibleRouteError`.
6. **Feasibility Enforcement:** If consecutive fueling opportunities exceed 500 miles or departure fuel cannot reach the first station, the API returns `422 Unprocessable Entity` specifying the exact gap.

---

## Quickstart & Setup

### Prerequisites
- Python 3.10+ (tested on Python 3.13)
- SQLite3 (included with Python)

### 1. Clone & Set Up Environment
```bash
# Clone the repository
git clone <your-repo-url>
cd "Backend 4 Spotter"

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
    "coordinate_precision": "Truck stops are mapped to city-corridor centroid coordinates derived from the US GNIS database with a 10-mile corridor radius buffer. Stations are not represented as exact pump GPS points.",
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

The test suite contains **35 unit and integration tests** verifying all requirements without depending on live network services:

```bash
python manage.py test tests
```

### Test Coverage Highlights:
- **`tests/test_optimizer.py`:** Short routes (<500 mi), medium routes (800 mi), long routes (1,400 mi), capacity fill-up trade-offs ($125 vs $175 bare-minimum), global vs local coordination ($85 vs $129 trap), closed-form cash verification ($160 exact), input validation parameter bounds, starting fuel below capacity (20 gal), 500-mile gap infeasibility, empty candidate dataset, and full-precision non-clamping physical conservation.
- **`tests/test_accounting.py`:** 10 MPG consumption verification, zero-stop trip accounting, multi-stop cost balancing, strict non-clamping negative balance rejection, and FIFO fuel inventory valuation with destination ending asset reconciliation.
- **`tests/test_corridor.py`:** Haversine distance accuracy, station filtering within 10-mile radius, exclusion of distant stations, mile marker ordering.
- **`tests/test_geocoding.py`:** Raw coordinate parsing, US boundary enforcement, local cache resolution, mocked Nominatim fallback and timeout handling.
- **`tests/test_fuel_data.py`:** Highway and exit parsing regex, Canadian province filtering.
- **`tests/test_api.py`:** Successful POST requests, parameter aliases (`start`/`finish`), missing payload validation, outside USA rejection, 504 timeouts, 404 route errors, and interactive map HTML rendering.

---

## Performance Benchmarks

Measured on standard development hardware:

| Journey | Distance | Time | External Calls | Refueling Stops |
|---|---|---|---|---|
| **Dallas, TX $\to$ Houston, TX** | 239.6 miles | **1.17 s** | 1 (OSRM) | 0 (direct) |
| **Chicago, IL $\to$ Atlanta, GA** | 685.0 miles | **1.58 s** | 1 (OSRM) | 2 stops |
| **New York, NY $\to$ Los Angeles, CA** | 2,800.6 miles | **3.66 s** | 1 (OSRM) | 13 stops |

---

## 5-Minute Loom Walkthrough Script

Use this structured outline when recording your submission video:

### 1. Introduction (0:00 – 0:45)
- State your name and the role: *Backend Django Engineer at Spotter AI*.
- State the objective: *An API that finds optimal, cost-effective fuel stops along a US driving route under a 500-mile vehicle range constraint at 10 MPG.*
- Highlight architectural pillars:
  1. *Continuous-State Linear Programming (HiGHS solver) guaranteeing global cost optimality and exact tank-state tracking.*
  2. *Single external routing API call per route.*
  3. *Offline geocoded truckstop database ensuring sub-second to low-latency response times.*

### 2. Live Postman Demonstration (0:45 – 2:30)
- **Request 1: Short Trip (Dallas to Houston, 240 mi):**
  - Send request. Point out execution time (~1.1 s).
  - Show that 0 fuel stops are required, but 24.0 gallons are consumed, and economic trip cost is correctly calculated (\$73.83).
- **Request 2: Medium Trip (Chicago to Atlanta, 685 mi):**
  - Send request. Show that 2 optimal stops were selected.
  - Show the driving legs: every leg is $\le 500$ miles (480.6 mi and 87.6 mi).
  - Point out how the optimizer skipped more expensive stations to refuel at Chattanooga for \$2.786/gal.
- **Request 3: Error Handling:**
  - Send an invalid request (e.g. coordinates in Paris, France). Show clear `400 Bad Request` with `LOCATION_OUTSIDE_USA`.

### 3. Visual Map Demonstration (2:30 – 3:30)
- Open browser to `http://127.0.0.1:8000/map/?origin=Chicago,+IL&destination=Atlanta,+GA`.
- Show the interactive Leaflet map:
  - Route polyline highlighted in blue.
  - Green origin pin, red destination pin.
  - Click on an orange fuel stop pin to show popup with station name, address, mile marker, retail price, and fill-up cost.
  - Show the sidebar dashboard metrics.

### 4. Code Architecture & Key Technical Decisions (3:30 – 4:45)
- Open VS Code and quickly showcase the key modules:
  - `api/models.py`: Point out `coordinate_precision` and multi-price variant tracking (`min_price`, `max_price`).
  - `api/services/corridor.py`: Point out the bounding-box filter and Haversine projection for fast corridor matching.
  - `api/services/optimizer.py`: Explain the Continuous Linear Programming formulation (HiGHS solver) and why it guarantees optimal fueling without greedy edge-case failures.
  - `api/services/accounting.py`: Explain the separation of fuel consumed vs. fuel purchased and FIFO inventory valuation.

### 5. Conclusion (4:45 – 5:00)
- Highlight the automated test suite: *35 comprehensive unit and integration tests passing in 0.11 seconds*.
- Conclude: *Thank you for your time, and I look forward to discussing the architecture further.*
