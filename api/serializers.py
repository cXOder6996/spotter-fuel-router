from rest_framework import serializers


class RouteOptimizationRequestSerializer(serializers.Serializer):
    """
    Validates request payload for route optimization.
    Supports either ('origin', 'destination') or ('start', 'finish').
    """
    origin = serializers.CharField(
        required=False,
        allow_blank=False,
        help_text="Starting city/state or coordinate pair (e.g. 'Dallas, TX' or '32.7767, -96.7970')"
    )
    destination = serializers.CharField(
        required=False,
        allow_blank=False,
        help_text="Finishing city/state or coordinate pair (e.g. 'Houston, TX' or '29.7604, -95.3698')"
    )
    start = serializers.CharField(required=False, allow_blank=False)
    finish = serializers.CharField(required=False, allow_blank=False)
    corridor_radius_miles = serializers.FloatField(
        required=False,
        default=10.0,
        min_value=1.0,
        max_value=50.0,
        help_text="Corridor search radius in miles from highway route (default 10 miles)"
    )
    price_policy = serializers.ChoiceField(
        choices=['min', 'avg', 'max'],
        default='min',
        required=False,
        help_text="Fuel price variant selection policy per station ('min', 'avg', 'max')"
    )

    def validate(self, attrs):
        origin = attrs.get('origin') or attrs.get('start')
        destination = attrs.get('destination') or attrs.get('finish')

        if not origin:
            raise serializers.ValidationError({
                'origin': "Please provide an origin or start location."
            })
        if not destination:
            raise serializers.ValidationError({
                'destination': "Please provide a destination or finish location."
            })

        attrs['origin'] = origin.strip()
        attrs['destination'] = destination.strip()
        return attrs
