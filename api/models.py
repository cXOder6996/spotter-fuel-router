from django.db import models


class TruckStop(models.Model):
    """
    Represents a physical truck stop / refueling station with verified location,
    pricing statistics, and fuel variant tracking.
    """
    opis_id = models.IntegerField(db_index=True, help_text="OPIS Truckstop ID")
    name = models.CharField(max_length=255, help_text="Truckstop Brand / Name")
    address = models.CharField(max_length=255, help_text="Highway junction / road address")
    city = models.CharField(max_length=100, db_index=True)
    state = models.CharField(max_length=10, db_index=True)
    rack_id = models.IntegerField(help_text="Wholesale fuel rack terminal ID")

    # Pricing metrics (handling multi-variant fuel reports)
    primary_price = models.FloatField(
        db_index=True,
        help_text="Primary price per gallon used for default routing (USD)"
    )
    min_price = models.FloatField(help_text="Lowest price reported among variants (USD)")
    max_price = models.FloatField(help_text="Highest price reported among variants (USD)")
    avg_price = models.FloatField(help_text="Average price reported among variants (USD)")
    price_variants = models.JSONField(
        default=list,
        help_text="List of all retail price quotes reported for this terminal"
    )

    # Geographic coordinates and provenance
    latitude = models.FloatField(db_index=True)
    longitude = models.FloatField(db_index=True)
    coordinate_precision = models.CharField(
        max_length=50,
        default='city_corridor_centroid',
        help_text="Precision classification of coordinates (e.g. city_corridor_centroid)"
    )
    coordinate_source = models.CharField(
        max_length=100,
        default='US_CITIES_GNIS_DATABASE',
        help_text="Data source used to obtain coordinate reference"
    )

    # Highway metadata parsed from address
    highway = models.CharField(max_length=50, blank=True, null=True, help_text="Interstate or highway identifier (e.g. I-44)")
    exit_number = models.CharField(max_length=50, blank=True, null=True, help_text="Highway exit number if present")

    class Meta:
        indexes = [
            models.Index(fields=['state', 'city']),
            models.Index(fields=['latitude', 'longitude']),
        ]
        ordering = ['opis_id']

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) - ${self.primary_price:.3f}/gal"
