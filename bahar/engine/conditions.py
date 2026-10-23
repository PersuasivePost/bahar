"""Per-venue hourly grid: forecast + exposure + local AQ + heat + sun + ground + traffic, as
HourConditions; and citing any of its values as Evidence."""

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from bahar.engine.evidence import EvidenceStore
from bahar.engine.geo import compass_word
from bahar.engine.ground import past_rain_mm
from bahar.engine.heat import heat_flag, wbgt_c
from bahar.engine.sun import light_class, sun_position
from bahar.engine.traffic import road_traffic_factor
from bahar.models import (
    HourAirQuality,
    HourConditions,
    HourExposure,
    SourceKey,
    Venue,
    WeatherHour,
)

_HALF_HOUR = timedelta(minutes=30)
_WMO_TEXT = {95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}
_FORECAST = SourceKey.open_meteo_forecast

# HourConditions field -> (label, unit, source, note)
_CITABLE: dict[str, tuple[str, str | None, SourceKey, str | None]] = {
    "wbgt": ("WBGT heat-stress index", "°C", SourceKey.engine,
             "modelled from forecast temperature, humidity, wind and sunshine (Liljegren 2008)"),
    "temp_c": ("air temperature", "°C", _FORECAST, None),
    "rain_prob": ("rain probability", "%", _FORECAST, None),
    "rain_mm": ("rain", "mm/h", _FORECAST, None),
    "wind_kmh": ("wind speed", "km/h", _FORECAST, None),
    "gust_kmh": ("wind gusts", "km/h", _FORECAST, None),
    "wind_dir_deg": ("wind from", None, _FORECAST, None),
    "uv": ("UV index", None, _FORECAST, None),
    "cape": ("storm energy (CAPE)", "J/kg", _FORECAST, None),
    "cloud": ("cloud cover", "%", _FORECAST, None),
    "weather_code": ("forecast weather", None, _FORECAST, None),
    "rain_24h": ("rain in the previous 24 h", "mm", _FORECAST, None),
    "rain_48h": ("rain in the previous 48 h", "mm", _FORECAST, None),
}  # fmt: skip


def hour_conditions(
    venue: Venue,
    wh: WeatherHour,
    ex: HourExposure,
    aq: HourAirQuality,
    weather_by_t: Mapping[datetime, WeatherHour],
) -> HourConditions | None:
    """One venue-hour, or None when the forecast lacks temperature / humidity / dew point."""
    if wh.temp_c is None or wh.rh is None or wh.dew_c is None:
        return None
    # Radiation is the mean of the preceding hour -> sun geometry at its midpoint; light and
    # the reported sun position describe the hour starting at t.
    elev_rad, _ = sun_position(venue.lat, venue.lon, wh.t - _HALF_HOUR)
    elev, az = sun_position(venue.lat, venue.lon, wh.t + _HALF_HOUR)
    wind = wh.wind_kmh or 0.0
    swr = wh.swr or 0.0
    cos_z = max(0.0, math.sin(math.radians(elev_rad)))  # cos(zenith) = sin(elevation)
    wbgt = wbgt_c(wh.temp_c, wh.rh, wh.dew_c, wind, swr, wh.direct_rad, cos_z, wh.wet_bulb_c)
    return HourConditions(
        venue_id=venue.id, t=wh.t, temp_c=wh.temp_c, rh=wh.rh, dew_c=wh.dew_c,
        wind_kmh=wind, wind_dir_deg=wh.wind_dir_deg or 0.0, gust_kmh=wh.gust_kmh or wind,
        rain_prob=wh.rain_prob or 0.0, rain_mm=wh.rain_mm or 0.0,
        weather_code=wh.weather_code or 0, cloud=wh.cloud or 0.0, uv=wh.uv or 0.0, swr=swr,
        cape=wh.cape or 0.0, is_day=bool(wh.is_day),
        pm25_bg=aq.pm25_bg, pm10_bg=aq.pm10_bg, bias=aq.bias,
        exposure=ex.exposure, top_source_ids=[c.evidence_id for c in ex.top],
        pm25_local=aq.pm25_local, pm25_low=aq.pm25_low, pm25_high=aq.pm25_high,
        pm10_local=aq.pm10_local, naqi=aq.naqi,
        wbgt=round(wbgt, 1), heat_flag=heat_flag(wbgt),
        sun_elev=round(elev, 1), sun_az=round(az, 1), light=light_class(elev),
        rain_24h=round(past_rain_mm(weather_by_t, wh.t, 24), 1),
        rain_48h=round(past_rain_mm(weather_by_t, wh.t, 48), 1),
        traffic_factor=road_traffic_factor(wh.t),
    )  # fmt: skip


def build_grid(
    venue: Venue,
    weather: Sequence[WeatherHour],
    exposure: Sequence[HourExposure],
    aq: Sequence[HourAirQuality],
    weather_by_t: Mapping[datetime, WeatherHour],
) -> dict[datetime, HourConditions]:
    """`weather`, `exposure` and `aq` are aligned hour by hour."""
    grid = {}
    for wh, ex, a in zip(weather, exposure, aq, strict=True):
        hc = hour_conditions(venue, wh, ex, a, weather_by_t)
        if hc is not None:
            grid[hc.t] = hc
    return grid


def cite(store: EvidenceStore, venue: Venue, hc: HourConditions, field: str,
         fetched_at: datetime) -> str:  # fmt: skip
    """Evidence id for one hourly value of the grid (same venue-hour-field -> same id)."""
    label, unit, source, note = _CITABLE[field]
    raw = getattr(hc, field)
    value: float | str
    if field == "wind_dir_deg":
        value, note = compass_word(raw), f"{raw:.0f}° (direction the wind blows from)"
    elif field == "weather_code":
        value = _WMO_TEXT.get(raw, f"WMO weather code {raw}")
    else:
        value = round(raw, 1)
    return store.value(f"{label} at {venue.name}, {hc.t:%a %H:%M}", value, unit, source,
                       fetched_at, note=note)  # fmt: skip


def cite_time(store: EvidenceStore, venue: Venue, t: datetime, what: str, fetched_at: datetime) -> str:
    """Evidence id for a forecast hour named in a label ("heat stress rises from 09:00")."""
    return store.value(f"{what} at {venue.name}, {t:%a}", f"{t:%H:%M}", "IST", _FORECAST, fetched_at)
