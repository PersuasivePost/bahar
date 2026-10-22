"""Ground-station PM2.5: WAQI (US-EPA AQI sub-indices, inverted to ug/m3), OpenAQ v3 fallback."""

import asyncio
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from bahar.config import sources as cfg
from bahar.config.settings import get_settings
from bahar.data import cache
from bahar.data.http import degraded, request_json
from bahar.data.result import Fetch
from bahar.engine.geo import haversine_km
from bahar.models import DegradedSource, Evidence, SourceKey, StationReading, Venue

IMPACT = "no ground-station bias correction: CAMS used as is, wider uncertainty band"
_KM_PER_DEG_LAT = 111.32

Candidate = tuple[str, str, float, float]  # (uid, name, lat, lon)


def pm25_from_epa_aqi(aqi: float) -> float | None:
    """Invert the US EPA PM2.5 AQI piecewise-linear breakpoints to ug/m3.

    AQI = (I_hi - I_lo) / (C_hi - C_lo) * (C - C_lo) + I_lo  =>  solve for C.
    Values above the table (AQI > 500) are not invertible; return None.
    """
    for c_lo, c_hi, i_lo, i_hi in cfg.EPA_PM25_BREAKPOINTS:
        if aqi <= i_hi:
            return c_lo + (aqi - i_lo) * (c_hi - c_lo) / (i_hi - i_lo) if aqi >= i_lo else c_lo
    return None


def _failure(source: SourceKey, reason: str, detail: str) -> DegradedSource:
    return degraded(source, "stations", reason, detail, IMPACT)  # type: ignore[arg-type]


def _bbox(venues: Sequence[Venue]) -> tuple[float, float, float, float]:
    """(south, west, north, east) around all venues, padded by the station radius."""
    pad_lat = cfg.STATION_RADIUS_KM / _KM_PER_DEG_LAT
    south = min(v.lat for v in venues) - pad_lat
    north = max(v.lat for v in venues) + pad_lat
    worst_cos = math.cos(math.radians(max(abs(south), abs(north))))
    pad_lon = cfg.STATION_RADIUS_KM / (_KM_PER_DEG_LAT * worst_cos)
    return south, min(v.lon for v in venues) - pad_lon, north, max(v.lon for v in venues) + pad_lon


def _pick_nearest(venues: Sequence[Venue], found: list[Candidate]) -> list[Candidate]:
    """Union over venues of their STATIONS_PER_VENUE nearest stations within the radius,
    nearest-first, capped at MAX_STATION_FEEDS."""
    best: dict[str, float] = {}
    by_uid = {c[0]: c for c in found}
    for v in venues:
        ranked = sorted(
            ((haversine_km(v.lat, v.lon, c[2], c[3]), c[0]) for c in found), key=lambda t: t[0]
        )
        for dist, uid in ranked[: cfg.STATIONS_PER_VENUE]:
            if dist <= cfg.STATION_RADIUS_KM:
                best[uid] = min(dist, best.get(uid, dist))
    return [by_uid[uid] for uid in sorted(best, key=best.__getitem__)[: cfg.MAX_STATION_FEEDS]]


def _fresh(observed: datetime, now: datetime) -> bool:
    return now - observed <= timedelta(hours=cfg.STATION_MAX_AGE_H)


# ------------------------------------------------------------------ WAQI


async def _waqi_feed(uid: str, fallback: Candidate, now: datetime) -> StationReading | None:
    data, _ = await request_json(
        SourceKey.waqi, "stations", IMPACT, "GET", cfg.WAQI_FEED_URL.format(uid=uid),
        params={"token": get_settings().waqi_token}, timeout_s=cfg.STATION_TIMEOUT_S,
    )  # fmt: skip
    try:
        feed = data["data"]
        aqi = float(feed["iaqi"]["pm25"]["v"])
        # Use `time.iso` (true offset). `time.v` is local wall time mislabelled as UTC, and the
        # bounds endpoint's station.time carries a bogus offset.
        observed = datetime.fromisoformat(feed["time"]["iso"])
        conc = pm25_from_epa_aqi(aqi)
        if conc is None or not _fresh(observed, now):
            return None
        _, name, lat, lon = fallback
        geo = feed.get("city", {}).get("geo")
        origins = ", ".join(a["name"] for a in feed.get("attributions", [])[:2] if "name" in a)
        return StationReading(
            uid=uid, name=feed.get("city", {}).get("name", name),
            lat=geo[0] if geo else lat, lon=geo[1] if geo else lon,
            pm25_ugm3=round(conc, 1), aqi_pm25=round(aqi), observed_at=observed,
            source="waqi", attribution=origins or None,
        )  # fmt: skip
    except (KeyError, TypeError, ValueError, IndexError):
        return None


async def _waqi(
    venues: Sequence[Venue], now: datetime
) -> tuple[list[StationReading], DegradedSource | None]:
    if not get_settings().waqi_token:
        return [], _failure(SourceKey.waqi, "empty", "WAQI_TOKEN not configured")
    south, west, north, east = _bbox(venues)
    data, failure = await request_json(
        SourceKey.waqi, "stations", IMPACT, "GET", cfg.WAQI_BOUNDS_URL,
        params={"latlng": f"{south},{west},{north},{east}", "token": get_settings().waqi_token},
        timeout_s=cfg.STATION_TIMEOUT_S,
    )  # fmt: skip
    if failure:
        return [], failure
    try:
        found = [
            (str(s["uid"]), s["station"]["name"], float(s["lat"]), float(s["lon"]))
            for s in data["data"]
            if s.get("aqi") not in (None, "-")
        ]
    except (KeyError, TypeError, ValueError):
        return [], _failure(SourceKey.waqi, "parse", "unexpected bounds response")
    chosen = _pick_nearest(venues, found)
    readings = await asyncio.gather(*(_waqi_feed(c[0], c, now) for c in chosen))
    fresh = [r for r in readings if r]
    if not fresh:
        detail = f"{len(found)} stations in box, none with a PM2.5 reading under {cfg.STATION_MAX_AGE_H:g} h old"
        return [], _failure(SourceKey.waqi, "empty", detail)
    return fresh, None


# ------------------------------------------------------------------ OpenAQ


async def _openaq_latest(
    loc: dict[str, Any], headers: dict[str, str], now: datetime
) -> StationReading | None:
    pm25_sensors = {
        s["id"]
        for s in loc.get("sensors", [])
        if s.get("parameter", {}).get("id") == cfg.OPENAQ_PM25_PARAMETER_ID
        and "g/m" in s["parameter"].get("units", "")  # ug/m3 only
    }
    data, _ = await request_json(
        SourceKey.openaq, "stations", IMPACT, "GET", cfg.OPENAQ_LATEST_URL.format(id=loc["id"]),
        headers=headers, timeout_s=cfg.STATION_TIMEOUT_S,
    )  # fmt: skip
    try:
        for row in data["results"]:
            if row["sensorsId"] not in pm25_sensors:
                continue
            observed = datetime.fromisoformat(row["datetime"]["utc"].replace("Z", "+00:00"))
            if not _fresh(observed, now):
                return None
            coords = loc["coordinates"]
            return StationReading(
                uid=str(loc["id"]), name=loc.get("name") or str(loc["id"]),
                lat=coords["latitude"], lon=coords["longitude"],
                pm25_ugm3=round(float(row["value"]), 1), observed_at=observed, source="openaq",
                attribution=(loc.get("provider") or {}).get("name"),
            )  # fmt: skip
    except (KeyError, TypeError, ValueError):
        return None
    return None


async def _openaq(
    venues: Sequence[Venue], now: datetime
) -> tuple[list[StationReading], DegradedSource | None]:
    key = get_settings().openaq_api_key
    if not key:
        return [], _failure(SourceKey.openaq, "empty", "OPENAQ_API_KEY not configured")
    headers = {"X-API-Key": key}
    south, west, north, east = _bbox(venues)
    data, failure = await request_json(
        SourceKey.openaq, "stations", IMPACT, "GET", cfg.OPENAQ_LOCATIONS_URL,
        params={
            "bbox": f"{west},{south},{east},{north}",  # OpenAQ order: minlon,minlat,maxlon,maxlat
            "parameters_id": cfg.OPENAQ_PM25_PARAMETER_ID, "limit": 100,
        },
        headers=headers, timeout_s=cfg.STATION_TIMEOUT_S,
    )  # fmt: skip
    if failure:
        return [], failure
    try:
        locs = {str(loc["id"]): loc for loc in data["results"]}
        found = [
            (uid, loc.get("name") or uid, loc["coordinates"]["latitude"], loc["coordinates"]["longitude"])
            for uid, loc in locs.items()
        ]
    except (KeyError, TypeError):
        return [], _failure(SourceKey.openaq, "parse", "unexpected locations response")
    chosen = _pick_nearest(venues, found)
    readings = await asyncio.gather(*(_openaq_latest(locs[c[0]], headers, now) for c in chosen))
    fresh = [r for r in readings if r]
    if not fresh:
        return [], _failure(SourceKey.openaq, "empty", f"{len(found)} locations, no fresh PM2.5")
    return fresh, None


# ------------------------------------------------------------------ entry point


async def fetch_stations(venues: Sequence[Venue]) -> Fetch[list[StationReading]]:
    key = cache.make_key("stations", pts=[(round(v.lat, 3), round(v.lon, 3)) for v in venues])
    return await cache.cached(key, cache.LIVE, lambda: _fetch(venues))


async def _fetch(venues: Sequence[Venue]) -> Fetch[list[StationReading]]:
    now = datetime.now(UTC)
    readings, waqi_failure = await _waqi(venues, now)
    failures = [waqi_failure] if waqi_failure else []
    if not readings:
        readings, openaq_failure = await _openaq(venues, now)
        if openaq_failure:
            failures.append(openaq_failure)
    evidence = [
        Evidence(
            id=f"raw:{r.source}:{r.uid}", label=f"PM2.5 at {r.name}", value=r.pm25_ugm3,
            unit="µg/m³", source=SourceKey(r.source), fetched_at=now,
            note=f"observed {r.observed_at.astimezone(cfg.IST):%d %b %H:%M} IST",
        )
        for r in readings
    ]  # fmt: skip
    return Fetch(value=readings, degraded=failures, evidence=evidence)
