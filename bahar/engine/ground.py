"""Ground condition from recent rain (Open-Meteo past_days + forecast precipitation)."""

from collections.abc import Mapping
from datetime import datetime, timedelta

from bahar.models import WeatherHour

_HOUR = timedelta(hours=1)


def past_rain_mm(hours: Mapping[datetime, WeatherHour], t: datetime, window_h: int) -> float:
    """Rain over the `window_h` hours up to t. Open-Meteo `precipitation` at hour h is the sum
    over the preceding hour, so hours t - window_h + 1 .. t are summed. Missing hours count 0."""
    total = 0.0
    for k in range(window_h):
        h = hours.get(t - k * _HOUR)
        if h is not None and h.rain_mm is not None:
            total += h.rain_mm
    return total
