"""Open-Meteo air quality (CAMS Global, 0.4 deg): all candidates in ONE request."""

from collections.abc import Sequence
from datetime import UTC, datetime

from bahar.config import sources as cfg
from bahar.data import cache
from bahar.data.http import degraded, request_json
from bahar.data.result import Fetch
from bahar.models import (
    AirQualityForecast,
    AirQualityHour,
    DegradedSource,
    SourceKey,
    Venue,
    VenueAirQuality,
)

IMPACT = "air quality unknown: no AQ gate or inhaled dose for the affected hours"


def _failure(reason: str, detail: str) -> DegradedSource:
    return degraded(SourceKey.open_meteo_air_quality, "airquality", reason, detail, IMPACT)  # type: ignore[arg-type]


def _parse_venue(venue: Venue, block: dict) -> VenueAirQuality:  # type: ignore[type-arg]
    h = block["hourly"]
    hours = [
        AirQualityHour(
            t=datetime.fromisoformat(stamp).replace(tzinfo=cfg.IST),
            pm25=h["pm2_5"][i], pm10=h["pm10"][i],
            no2=h["nitrogen_dioxide"][i], o3=h["ozone"][i],
        )
        for i, stamp in enumerate(h["time"])
    ]  # fmt: skip
    return VenueAirQuality(venue_id=venue.id, hours=hours)


async def fetch_air_quality(
    venues: Sequence[Venue], need_until: datetime | None = None
) -> Fetch[AirQualityForecast]:
    """`need_until`: last hour the caller wants; a shorter CAMS horizon is reported as
    `horizon` degradation (the hours beyond it stay null = unknown)."""
    key = cache.make_key("aq", pts=[(v.id, round(v.lat, 3), round(v.lon, 3)) for v in venues])
    result = await cache.cached(key, cache.LIVE, lambda: _fetch(venues))
    if result.value and need_until:
        horizon = max(
            (h.t for vq in result.value.venues.values() for h in vq.hours if h.pm25 is not None),
            default=None,
        )
        if horizon is None or horizon < need_until:
            result = result.model_copy(
                update={"degraded": [_failure("horizon", f"CAMS forecast ends {horizon}")]}
            )
    return result


async def _fetch(venues: Sequence[Venue]) -> Fetch[AirQualityForecast]:
    data, failure = await request_json(
        SourceKey.open_meteo_air_quality, "airquality", IMPACT, "GET", cfg.AIR_QUALITY_URL,
        params={
            "latitude": ",".join(f"{v.lat:.4f}" for v in venues),
            "longitude": ",".join(f"{v.lon:.4f}" for v in venues),
            "hourly": ",".join(cfg.AIR_QUALITY_HOURLY),
            "timezone": cfg.TIMEZONE,
            "forecast_days": cfg.AQ_FORECAST_DAYS,
            "past_days": cfg.AQ_PAST_DAYS,
        },
    )  # fmt: skip
    if failure:
        return Fetch(degraded=[failure])
    blocks = data if isinstance(data, list) else [data]
    if len(blocks) != len(venues):
        detail = f"{len(blocks)} locations returned for {len(venues)} venues"
        return Fetch(degraded=[_failure("parse", detail)])
    try:
        forecast = AirQualityForecast(
            fetched_at=datetime.now(UTC),
            venues={v.id: _parse_venue(v, b) for v, b in zip(venues, blocks, strict=True)},
        )
    except (KeyError, TypeError, ValueError) as exc:
        return Fetch(degraded=[_failure("parse", repr(exc)[:200])])
    return Fetch(value=forecast)
