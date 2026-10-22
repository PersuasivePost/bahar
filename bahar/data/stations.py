"""Ground-station PM2.5: WAQI (US-EPA AQI sub-indices, inverted to ug/m3), OpenAQ v3 fallback.

Locations are filtered by last-report time BEFORE the nearest ones are chosen, so a fresh
monitor further away is not hidden behind nearer stations that stopped reporting.
"""

import asyncio
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from bahar.config import sources as cfg
from bahar.config.settings import get_settings
from bahar.data import cache
from bahar.data.http import degraded, request_json
from bahar.data.result import Fetch
from bahar.engine.geo import haversine_km
from bahar.models import (
    DegradedSource,
    Evidence,
    SourceKey,
    StaleStation,
    StationReading,
    StationSet,
    Venue,
)

IMPACT = "no ground-station bias correction: CAMS used as is, wider uncertainty band"
_KM_PER_DEG_LAT = 111.32


class Candidate(NamedTuple):
    uid: str
    name: str
    lat: float
    lon: float
    last_seen: datetime | None  # None = unknown (kept; the feed re-checks)
    provider: str | None = None
    regulatory: bool = False


def pm25_from_epa_aqi(aqi: float) -> float | None:
    """Invert the US EPA PM2.5 AQI piecewise-linear breakpoints to ug/m3.

    AQI = (I_hi - I_lo) / (C_hi - C_lo) * (C - C_lo) + I_lo  =>  solve for C.
    Values above the table (AQI > 500) are not invertible; return None.
    """
    for c_lo, c_hi, i_lo, i_hi in cfg.EPA_PM25_BREAKPOINTS:
        if aqi <= i_hi:
            return c_lo + (aqi - i_lo) * (c_hi - c_lo) / (i_hi - i_lo) if aqi >= i_lo else c_lo
    return None


def is_regulatory(*texts: str | None) -> bool:
    return any(t and cfg.REGULATORY_PATTERN.search(t) for t in texts)


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


def _distance_km(venues: Sequence[Venue], c: Candidate) -> float:
    return min(haversine_km(v.lat, v.lon, c.lat, c.lon) for v in venues)


def _fresh(observed: datetime | None, now: datetime) -> bool:
    return observed is None or now - observed <= timedelta(hours=cfg.STATION_MAX_AGE_H)


def _pick_nearest(venues: Sequence[Venue], found: list[Candidate]) -> list[Candidate]:
    """Union over venues of their STATIONS_PER_VENUE nearest stations within the radius,
    nearest-first, capped at MAX_STATION_FEEDS. Callers pass fresh candidates only."""
    best: dict[str, float] = {}
    by_uid = {c.uid: c for c in found}
    for v in venues:
        ranked = sorted((haversine_km(v.lat, v.lon, c.lat, c.lon), c.uid) for c in found)
        for dist, uid in ranked[: cfg.STATIONS_PER_VENUE]:
            if dist <= cfg.STATION_RADIUS_KM:
                best[uid] = min(dist, best.get(uid, dist))
    return [by_uid[uid] for uid in sorted(best, key=best.__getitem__)[: cfg.MAX_STATION_FEEDS]]


def _stale_regulatory(
    venues: Sequence[Venue], found: list[Candidate], now: datetime
) -> list[StaleStation]:
    stale = [
        (d, c)
        for c in found
        if c.regulatory and not _fresh(c.last_seen, now)
        and (d := _distance_km(venues, c)) <= cfg.STATION_RADIUS_KM
    ]
    stale.sort(key=lambda t: t[0])
    return [
        StaleStation(
            name=c.name, provider=c.provider, regulatory=True,
            last_seen=c.last_seen, distance_km=round(d, 1),  # type: ignore[arg-type]
        )
        for d, c in stale[: cfg.MAX_STALE_REPORTED]
    ]  # fmt: skip


class _Result(NamedTuple):
    readings: list[StationReading]
    stale: list[StaleStation]
    failure: DegradedSource | None


# ------------------------------------------------------------------ WAQI


def _parse_time(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


async def _waqi_feed(c: Candidate, now: datetime) -> StationReading | None:
    data, _ = await request_json(
        SourceKey.waqi, "stations", IMPACT, "GET", cfg.WAQI_FEED_URL.format(uid=c.uid),
        params={"token": get_settings().waqi_token}, timeout_s=cfg.STATION_TIMEOUT_S,
    )  # fmt: skip
    try:
        feed = data["data"]
        aqi = float(feed["iaqi"]["pm25"]["v"])
        # `time.iso` carries the true offset; `time.v` is local wall time mislabelled as UTC.
        observed = datetime.fromisoformat(feed["time"]["iso"])
        conc = pm25_from_epa_aqi(aqi)
        if conc is None or not _fresh(observed, now):
            return None
        geo = feed.get("city", {}).get("geo")
        name = feed.get("city", {}).get("name", c.name)
        # The first attribution is the originator; the last is always WAQI itself.
        origins = [a["name"] for a in feed.get("attributions", []) if "name" in a]
        provider = origins[0] if origins else None
        return StationReading(
            uid=c.uid, name=name,
            lat=geo[0] if geo else c.lat, lon=geo[1] if geo else c.lon,
            pm25_ugm3=round(conc, 1), aqi_pm25=round(aqi), observed_at=observed,
            source="waqi", provider=provider, regulatory=is_regulatory(name, *origins),
        )  # fmt: skip
    except (KeyError, TypeError, ValueError, IndexError):
        return None


async def _waqi(venues: Sequence[Venue], now: datetime) -> _Result:
    if not get_settings().waqi_token:
        return _Result([], [], _failure(SourceKey.waqi, "empty", "WAQI_TOKEN not configured"))
    south, west, north, east = _bbox(venues)
    data, failure = await request_json(
        SourceKey.waqi, "stations", IMPACT, "GET", cfg.WAQI_BOUNDS_URL,
        params={"latlng": f"{south},{west},{north},{east}", "token": get_settings().waqi_token},
        timeout_s=cfg.STATION_TIMEOUT_S,
    )  # fmt: skip
    if failure:
        return _Result([], [], failure)
    try:
        found = [
            Candidate(
                str(s["uid"]), s["station"]["name"], float(s["lat"]), float(s["lon"]),
                # bounds `station.time` is an aware timestamp; good enough to pre-filter
                _parse_time(s["station"].get("time")),
                regulatory=is_regulatory(s["station"]["name"]),
            )
            for s in data["data"]
            if s.get("aqi") not in (None, "-")
        ]  # fmt: skip
    except (KeyError, TypeError, ValueError):
        return _Result([], [], _failure(SourceKey.waqi, "parse", "unexpected bounds response"))
    fresh = [c for c in found if _fresh(c.last_seen, now)]
    readings = await asyncio.gather(*(_waqi_feed(c, now) for c in _pick_nearest(venues, fresh)))
    kept = [r for r in readings if r]
    stale = _stale_regulatory(venues, found, now)
    if not kept:
        detail = f"{len(found)} stations in box, none with PM2.5 under {cfg.STATION_MAX_AGE_H:g} h old"
        return _Result([], stale, _failure(SourceKey.waqi, "empty", detail))
    return _Result(kept, stale, None)


# ------------------------------------------------------------------ OpenAQ


def _openaq_candidate(loc: dict[str, Any]) -> Candidate:
    provider = (loc.get("provider") or {}).get("name")
    owner = (loc.get("owner") or {}).get("name")
    named = [p for p in (provider, owner) if p and p not in ("N/A", "Unknown Governmental Organization")]
    last = (loc.get("datetimeLast") or {}).get("utc")
    coords = loc["coordinates"]
    return Candidate(
        uid=str(loc["id"]), name=loc.get("name") or str(loc["id"]),
        lat=coords["latitude"], lon=coords["longitude"],
        last_seen=datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None,
        provider=" / ".join(dict.fromkeys(named)) or None,
        # isMonitor = OpenAQ's reference-grade flag; the pattern catches CPCB-network
        # stations mis-flagged as sensors (e.g. "Bandra Kurla Complex - IITM").
        regulatory=bool(loc.get("isMonitor")) or is_regulatory(provider, owner, loc.get("name")),
    )  # fmt: skip


async def _openaq_latest(
    loc: dict[str, Any], c: Candidate, headers: dict[str, str], now: datetime
) -> StationReading | None:
    pm25_sensors = {
        s["id"]
        for s in loc.get("sensors", [])
        if s.get("parameter", {}).get("id") == cfg.OPENAQ_PM25_PARAMETER_ID
        and "g/m" in s["parameter"].get("units", "")  # ug/m3 only
    }
    data, _ = await request_json(
        SourceKey.openaq, "stations", IMPACT, "GET", cfg.OPENAQ_LATEST_URL.format(id=c.uid),
        headers=headers, timeout_s=cfg.STATION_TIMEOUT_S,
    )  # fmt: skip
    try:
        for row in data["results"]:
            if row["sensorsId"] not in pm25_sensors:
                continue
            observed = datetime.fromisoformat(row["datetime"]["utc"].replace("Z", "+00:00"))
            value = float(row["value"])
            if not _fresh(observed, now) or value < 0:
                return None
            return StationReading(
                uid=c.uid, name=c.name, lat=c.lat, lon=c.lon,
                pm25_ugm3=round(value, 1), observed_at=observed, source="openaq",
                provider=c.provider, regulatory=c.regulatory,
            )  # fmt: skip
    except (KeyError, TypeError, ValueError):
        return None
    return None


async def _openaq(venues: Sequence[Venue], now: datetime) -> _Result:
    key = get_settings().openaq_api_key
    if not key:
        return _Result([], [], _failure(SourceKey.openaq, "empty", "OPENAQ_API_KEY not configured"))
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
        return _Result([], [], failure)
    try:
        locs = {str(loc["id"]): loc for loc in data["results"] if not loc.get("isMobile")}
        found = [_openaq_candidate(loc) for loc in locs.values()]
    except (KeyError, TypeError, ValueError):
        return _Result([], [], _failure(SourceKey.openaq, "parse", "unexpected locations response"))
    fresh = [c for c in found if _fresh(c.last_seen, now)]
    chosen = _pick_nearest(venues, fresh)
    readings = await asyncio.gather(*(_openaq_latest(locs[c.uid], c, headers, now) for c in chosen))
    kept = [r for r in readings if r]
    stale = _stale_regulatory(venues, found, now)
    if not kept:
        detail = f"{len(found)} locations ({len(fresh)} fresh), no PM2.5 under {cfg.STATION_MAX_AGE_H:g} h old"
        return _Result([], stale, _failure(SourceKey.openaq, "empty", detail))
    return _Result(kept, stale, None)


# ------------------------------------------------------------------ entry point


async def fetch_stations(venues: Sequence[Venue]) -> Fetch[StationSet]:
    key = cache.make_key("stations", pts=[(round(v.lat, 3), round(v.lon, 3)) for v in venues])
    # Empty answers are cached too (15 min): Mumbai's WAQI box is empty for hours at a time.
    return await cache.cached(key, cache.LIVE, lambda: _fetch(venues), keep_empty=True)


async def _fetch(venues: Sequence[Venue]) -> Fetch[StationSet]:
    now = datetime.now(UTC)
    waqi = await _waqi(venues, now)
    readings, stale = waqi.readings, waqi.stale
    failures = [waqi.failure] if waqi.failure else []
    if not readings:
        openaq = await _openaq(venues, now)
        readings, stale = openaq.readings, stale + openaq.stale
        if openaq.failure:
            failures.append(openaq.failure)
    if readings:
        stale = []  # only reported when nothing fresh exists
    evidence = [
        Evidence(
            id=f"raw:{r.source}:{r.uid}", label=f"PM2.5 measured at {r.name}", value=r.pm25_ugm3,
            unit="µg/m³", source=SourceKey(r.source), fetched_at=now,
            note=f"{r.provider or 'unknown provider'}; "
            f"{'regulatory monitor' if r.regulatory else 'low-cost sensor'}; "
            f"observed {r.observed_at.astimezone(cfg.IST):%d %b %H:%M} IST",
        )
        for r in readings
    ]  # fmt: skip
    value = StationSet(
        fetched_at=now, readings=readings,
        stale=sorted(stale, key=lambda s: s.distance_km)[: cfg.MAX_STALE_REPORTED],
    )  # fmt: skip
    return Fetch(value=value, degraded=failures, evidence=evidence)
