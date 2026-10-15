import time
import logging
from django.shortcuts import render
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from api.serializers import RouteOptimizationRequestSerializer
from api.services.geocoding import geocode_location, GeocodingError, LocationOutsideUSAError
from api.services.routing import get_driving_route, RoutingError, RouteNotFoundError, RoutingTimeoutError
from api.services.corridor import find_corridor_truck_stops
from api.services.optimizer import optimize_fuel_stops, InfeasibleRouteError
from api.services.accounting import calculate_fuel_accounting

logger = logging.getLogger(__name__)


def run_route_optimization_pipeline(
    origin_str: str,
    destination_str: str,
    corridor_radius: float = 10.0,
    price_policy: str = 'min',
    starting_tank_gallons: float = 50.0
):
    """
    Executes the full pipeline:
      1. Geocodes origin & destination (with US validation)
      2. Fetches driving route from OSRM (1 call)
      3. Filters candidate truck stops within route corridor
      4. Optimizes refueling stops with 500-mile range constraint & tank state tracking
      5. Calculates separated fuel consumption and economic accounting
    """
    t_start = time.perf_counter()

    # Step 1: Geocoding
    orig_lat, orig_lon, orig_name = geocode_location(origin_str)
    dest_lat, dest_lon, dest_name = geocode_location(destination_str)

    # Step 2: Driving route
    route_data = get_driving_route(orig_lat, orig_lon, dest_lat, dest_lon)
    total_dist = route_data['distance_miles']
    duration_hrs = route_data['duration_hours']
    geometry = route_data['geojson_geometry']
    coords = route_data['coordinates']

    # Step 3: Corridor candidate extraction
    candidate_stops = find_corridor_truck_stops(coords, corridor_radius_miles=corridor_radius)

    # Benchmark fuel price for reference
    ref_price = 3.50
    if candidate_stops:
        ref_price = round(sum(s['station'].primary_price for s in candidate_stops) / len(candidate_stops), 4)

    # Step 4: Optimization (Continuous Linear Programming with HiGHS)
    opt_result = optimize_fuel_stops(
        total_distance_miles=total_dist,
        candidate_stops=candidate_stops,
        max_range_miles=getattr(settings, 'VEHICLE_MAX_RANGE_MILES', 500.0),
        fuel_economy_mpg=getattr(settings, 'VEHICLE_FUEL_ECONOMY_MPG', 10.0),
        tank_capacity_gallons=getattr(settings, 'VEHICLE_TANK_CAPACITY_GALLONS', 50.0),
        initial_tank_gallons=starting_tank_gallons,
        initial_fuel_price_estimate=ref_price,
    )

    # Step 5: Accounting (Strict physical tank reconciliation)
    accounting = calculate_fuel_accounting(
        total_distance_miles=total_dist,
        selected_stops=opt_result['selected_stops'],
        legs=opt_result['legs'],
        fuel_economy_mpg=getattr(settings, 'VEHICLE_FUEL_ECONOMY_MPG', 10.0),
        initial_tank_gallons=starting_tank_gallons,
        reference_fuel_price=ref_price,
    )

    elapsed_ms = round((time.perf_counter() - t_start) * 1000.0, 1)

    response_payload = {
        'status': 'success',
        'trip_summary': {
            'origin': {
                'query': origin_str,
                'resolved_name': orig_name,
                'coordinates': {'latitude': orig_lat, 'longitude': orig_lon},
            },
            'destination': {
                'query': destination_str,
                'resolved_name': dest_name,
                'coordinates': {'latitude': dest_lat, 'longitude': dest_lon},
            },
            'total_distance_miles': total_dist,
            'estimated_duration_hours': duration_hrs,
            'pipeline_execution_time_ms': elapsed_ms,
        },
        'fuel_economics': {
            'vehicle_parameters': {
                'max_range_miles': getattr(settings, 'VEHICLE_MAX_RANGE_MILES', 500.0),
                'fuel_economy_mpg': getattr(settings, 'VEHICLE_FUEL_ECONOMY_MPG', 10.0),
                'tank_capacity_gallons': getattr(settings, 'VEHICLE_TANK_CAPACITY_GALLONS', 50.0),
                'initial_tank_state': f"{starting_tank_gallons:.1f} gal ({starting_tank_gallons * getattr(settings, 'VEHICLE_FUEL_ECONOMY_MPG', 10.0):.1f} mi range at departure)",
            },
            'total_fuel_consumed_gallons': accounting['total_fuel_consumed_gallons'],
            'total_fuel_purchased_during_trip_gallons': accounting['fuel_purchased_during_trip_gallons'],
            'total_cash_expenditure_at_pumps_usd': accounting['cash_expenditure_at_pumps_usd'],
            'economic_cost_of_fuel_consumed_usd': accounting['estimated_total_fuel_cost_usd'],
            'destination_remaining_fuel_gallons': accounting['destination_remaining_fuel_gallons'],
            'estimated_total_fuel_cost_usd': accounting['estimated_total_fuel_cost_usd'],
            'inventory_fuel_accounting': accounting.get('inventory_fuel_accounting', {}),
            'initial_fuel_accounting': accounting['initial_fuel_accounting'],
        },
        'refueling_plan': {
            'stops_count': accounting['fuel_stops_count'],
            'stops': accounting['fuel_stops'],
            'driving_legs': accounting['route_legs'],
        },
        'route_geometry': geometry,
        'assumptions_and_limitations': {
            'vehicle_model': 'Standard heavy truck with 500-mile max range and uniform 10 MPG fuel economy.',
            'starting_fuel_rule': f"Vehicle departs with {starting_tank_gallons:.1f} gallons in tank. Cash expenditure covers fuel bought during the journey; estimated total fuel cost includes valuation of starting fuel consumed.",
            'coordinate_precision': 'Truck stops are mapped to verified highway corridor coordinates derived from national geographic gazetteer and interstate junction reference (city corridor centroids). Stations are not represented as exact pump GPS points.',
            'fuel_pricing': f"Default price selection policy '{price_policy}'. Multiple prices per terminal reflect reported fuel grade variants; station price ranges are documented per stop.",
            'routing_provider': 'Open Source Routing Machine (OSRM) driving profile via public endpoint.',
        },
        'warnings': opt_result.get('warnings', []),
    }

    return response_payload


class OptimizeRouteView(APIView):
    """
    POST /api/v1/optimize-route/
    Calculates driving route, identifies optimal cost-effective fuel stops within
    a 500-mile range constraint, and computes complete fuel accounting.
    """
    def post(self, request):
        serializer = RouteOptimizationRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    'status': 'error',
                    'error_type': 'VALIDATION_ERROR',
                    'errors': serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        origin = serializer.validated_data['origin']
        destination = serializer.validated_data['destination']
        radius = serializer.validated_data.get('corridor_radius_miles', 10.0)
        policy = serializer.validated_data.get('price_policy', 'min')
        starting_tank = serializer.validated_data.get('starting_tank_gallons', 50.0)

        try:
            result = run_route_optimization_pipeline(
                origin,
                destination,
                corridor_radius=radius,
                price_policy=policy,
                starting_tank_gallons=starting_tank
            )
            return Response(result, status=status.HTTP_200_OK)

        except LocationOutsideUSAError as e:
            return Response(
                {
                    'status': 'error',
                    'error_type': 'LOCATION_OUTSIDE_USA',
                    'message': str(e),
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        except GeocodingError as e:
            return Response(
                {
                    'status': 'error',
                    'error_type': 'GEOCODING_FAILED',
                    'message': str(e),
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        except RouteNotFoundError as e:
            return Response(
                {
                    'status': 'error',
                    'error_type': 'ROUTE_NOT_FOUND',
                    'message': str(e),
                },
                status=status.HTTP_404_NOT_FOUND
            )
        except RoutingTimeoutError as e:
            return Response(
                {
                    'status': 'error',
                    'error_type': 'ROUTING_TIMEOUT',
                    'message': str(e),
                },
                status=status.HTTP_504_GATEWAY_TIMEOUT
            )
        except InfeasibleRouteError as e:
            return Response(
                {
                    'status': 'error',
                    'error_type': 'INFEASIBLE_ROUTE',
                    'message': str(e),
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:
            logger.exception("Unexpected error during route optimization")
            return Response(
                {
                    'status': 'error',
                    'error_type': 'SERVER_ERROR',
                    'message': f"Internal server error: {str(e)}",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def get(self, request):
        """
        Supports GET for convenient manual testing via browser or query params.
        """
        serializer = RouteOptimizationRequestSerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response(
                {
                    'status': 'error',
                    'error_type': 'VALIDATION_ERROR',
                    'errors': serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        return self.post(request)


class InteractiveMapView(APIView):
    """
    GET /map/?origin=Dallas,+TX&destination=Houston,+TX
    Renders an interactive Leaflet.js map with driving route polyline,
    origin/destination markers, and clickable fuel stop popups.
    """
    def get(self, request):
        origin = request.GET.get('origin', 'Dallas, TX')
        destination = request.GET.get('destination', 'Houston, TX')
        radius = float(request.GET.get('radius', 10.0))

        import json
        try:
            data = run_route_optimization_pipeline(origin, destination, corridor_radius=radius)
            context = {
                'data': data,
                'route_geometry_json': json.dumps(data.get('route_geometry', {})),
                'origin': origin,
                'destination': destination,
                'error': None,
            }
        except Exception as e:
            context = {
                'data': None,
                'route_geometry_json': '{}',
                'origin': origin,
                'destination': destination,
                'error': str(e),
            }

        return render(request, 'map.html', context)
