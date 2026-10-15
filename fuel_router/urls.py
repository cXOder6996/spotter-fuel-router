from django.contrib import admin
from django.urls import path, include
from api.views import InteractiveMapView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('api.urls')),
    path('', InteractiveMapView.as_view(), name='home-map'),
    path('map/', InteractiveMapView.as_view(), name='map-view'),
]
