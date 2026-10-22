"""Open-Meteo forecast: all candidates in ONE request, IST, with the past 2 days of rain."""

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from bahar.config import sources as cfg
from bahar.data import cache
from bahar.data.http import degraded, request_json
from bahar.data.result import Fetch
from bahar.models import (
    DegradedSource,
    Forecast,
    SourceKey,
    Venue,
    VenueWeather,
    WeatherHour,
)

IMPACT = "no weather: no venue can be scored; recommendation unavailable"
_REJECTED = re.compile(r"invalid String value (\w+)")

# Open-Meteo variable -> WeatherHour field
_FIELDS = {
    "temperature_2m": "temp_c", "relative_humidity_2m": "rh", "dew_point_2m": "dew_c",
    "apparent_temperature": "apparent_c", "wet_bulb_temperature_2m": "wet_bulb_c",
    "precipitation_probability": "rain_prob", "precipitation": "rain_mm",
    "weather_code": "weather_code", "cloud_cover": "cloud", "wind_speed_10m": "wind_kmh",
    "wind_direction_10m": "wind_dir_deg", "wind_gusts_10m": "gust_kmh", "uv_index": "uv",
    "shortwave_radiation": "swr", "direct_radiation": "direct_rad", "cape": "cape",
    "is_day": "is_day",
}  # fmt: skip


def _ist(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=cfg.IST)


def _parse_venue(venue: Venue, block: dict[str, Any]) -> VenueWeather:
    hourly = block["hourly"]
    hours = []
    for i, stamp in enumerate(hourly["time"]):
        row: dict[str, Any] = {
            field: hourly[var][i] for var, field in _FIELDS.items() if var in hourly
        }
        if row.get("is_day") is not None:
            row["is_day"] = bool(row["is_day"])
        hours.append(WeatherHour(t=_ist(stamp), **row))
    daily = block.get("daily", {})
    return VenueWeather(
        venue_id=venue.id,
        lat=block["latitude"],
        lon=block["longitude"],
        elevation_m=block.get("elevation"),
        hours=hours,
        sunrise={date.fromisoformat(d[:10]): _ist(d) for d in daily.get("sunrise", []) if d},
        sunset={date.fromisoformat(d[:10]): _ist(d) for d in daily.get("sunset", []) if d},
    )


async def fetch_forecast(venues: Sequence[Venue]) -> Fetch[Forecast]:
    key = cache.make_key(
        "forecast", pts=[(v.id, round(v.lat, 3), round(v.lon, 3)) for v in venues]
    )
    return await cache.cached(key, cache.LIVE, lambda: _fetch(venues))


async def _fetch(venues: Sequence[Venue]) -> Fetch[Forecast]:
    variables = list(cfg.FORECAST_HOURLY)
    dropped: list[str] = []
    for _ in range(3):  # drop variables the API rejects, then retry
        data, failure = await request_json(
            SourceKey.open_meteo_forecast, "forecast", IMPACT, "GET", cfg.FORECAST_URL,
            params={
                "latitude": ",".join(f"{v.lat:.4f}" for v in venues),
                "longitude": ",".join(f"{v.lon:.4f}" for v in venues),
                "hourly": ",".join(variables),
                "daily": "sunrise,sunset",
                "timezone": cfg.TIMEZONE,
                "past_days": cfg.PAST_DAYS,
                "forecast_days": cfg.FORECAST_DAYS,
            },
        )  # fmt: skip
        rejected = _REJECTED.search(failure.detail) if failure else None
        if rejected and rejected.group(1) in variables:
            variables.remove(rejected.group(1))
            dropped.append(rejected.group(1))
            continue
        break
    if failure:
        return Fetch(degraded=[failure])

    blocks = data if isinstance(data, list) else [data]  # one location -> bare object
    if len(blocks) != len(venues):
        detail = f"{len(blocks)} locations returned for {len(venues)} venues"
        return Fetch(degraded=[_parse_failure(detail)])
    try:
        forecast = Forecast(
            fetched_at=datetime.now(UTC),
            venues={v.id: _parse_venue(v, b) for v, b in zip(venues, blocks, strict=True)},
        )
    except (KeyError, TypeError, ValueError) as exc:
        return Fetch(degraded=[_parse_failure(repr(exc)[:200])])
    return Fetch(value=forecast)


def _parse_failure(detail: str) -> DegradedSource:
    return degraded(SourceKey.open_meteo_forecast, "forecast", "parse", detail, IMPACT)
