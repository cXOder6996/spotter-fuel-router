from django.urls import path
from api.views import OptimizeRouteView, InteractiveMapView

urlpatterns = [
    # Required REST API contract per spec: POST /api/v1/optimize-route/
    path('v1/optimize-route/', OptimizeRouteView.as_view(), name='optimize-route'),

    # Interactive visual map view
    path('map/', InteractiveMapView.as_view(), name='interactive-map'),
]
