"""Orchestration: fetch everything the engine needs for a parsed query, in parallel where the
data allows, with per-stage latency. (The LLM stages are wired in Step 7.)"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from bahar.config.activities import ACTIVITIES, normalize_activity
from bahar.config.sources import IST, MAX_RADIUS_KM
from bahar.data.airquality import fetch_air_quality
from bahar.data.geocode import geocode, search_radius_km
from bahar.data.overpass import fetch_context, fetch_venues
from bahar.data.routing import fetch_travel
from bahar.data.stations import fetch_stations
from bahar.data.weather import fetch_forecast
from bahar.engine.sun import sun_days
from bahar.engine.timewindow import resolve_window
from bahar.models import Activity, DegradedSource, EngineInput, ParsedQuery

GatherStatus = Literal["ok", "out_of_scope", "needs_clarification", "no_venues", "no_forecast"]


class Gathered(BaseModel):
    status: GatherStatus
    detail: str = ""
    engine_input: EngineInput | None = None
    degraded: list[DegradedSource] = []
    latency_ms: dict[str, int] = {}


def _activity(parsed: ParsedQuery) -> Activity | None:
    if parsed.activity is not None:
        return parsed.activity
    return normalize_activity(parsed.activity_raw) if parsed.activity_raw else None


async def gather_inputs(parsed: ParsedQuery, now: datetime) -> Gathered:
    """geocode -> venues -> (context | forecast | air quality | stations | routing)."""
    latency: dict[str, int] = {}
    degraded: list[DegradedSource] = []
    activity = _activity(parsed)
    if not parsed.in_scope or activity is None:
        return Gathered(status="out_of_scope", detail="no supported activity")
    location = parsed.location_text or (parsed.commute.from_ if parsed.commute else None)
    if not location:
        return Gathered(status="needs_clarification", detail="location missing")

    t0 = time.perf_counter()
    g = await geocode(location)
    latency["geocode"] = _ms(t0)
    degraded += g.degraded
    place = g.value
    if place is None:
        return Gathered(status="needs_clarification", detail=f"could not locate {location!r}",
                        degraded=degraded, latency_ms=latency)  # fmt: skip

    profile = ACTIVITIES[activity]
    radius = min(parsed.radius_km, MAX_RADIUS_KM) if parsed.radius_km else search_radius_km(place, profile.radius_km)
    t0 = time.perf_counter()
    vf = await fetch_venues(activity, place.lat, place.lon, radius)
    latency["venues"] = _ms(t0)
    degraded += vf.degraded
    if not vf.value:
        return Gathered(status="no_venues", detail=f"no {activity.value} venues within {radius:g} km",
                        degraded=degraded, latency_ms=latency)  # fmt: skip
    venues = vf.value

    now = now.astimezone(IST)
    days = [now.date() + timedelta(days=k) for k in range(8)]
    sun = sun_days(place.lat, place.lon, days)
    window = resolve_window(
        parsed.time, now, {d: s.sunrise for d, s in sun.items()}, {d: s.sunset for d, s in sun.items()}
    )
    need_until = (window.starts[-1] if window.starts else now) + timedelta(minutes=profile.duration_min)

    t0 = time.perf_counter()
    ctx_f, fc_f, aq_f, st_f, tr_f = await asyncio.gather(
        fetch_context(venues), fetch_forecast(venues), fetch_air_quality(venues, need_until=need_until),
        fetch_stations(venues), fetch_travel((place.lat, place.lon), venues),
    )  # fmt: skip
    latency["parallel_fetch"] = _ms(t0)
    for name, f in (("context", ctx_f), ("forecast", fc_f), ("airquality", aq_f),
                    ("stations", st_f), ("routing", tr_f)):  # fmt: skip
        latency[f"fetch_{name}"] = f.elapsed_ms  # inside parallel_fetch
        degraded += f.degraded
    if fc_f.value is None:
        return Gathered(status="no_forecast", detail="weather forecast unavailable",
                        degraded=degraded, latency_ms=latency)  # fmt: skip

    inp = EngineInput(
        parsed=parsed, activity=activity, place=place, radius_km=radius, venues=venues,
        window=window, context=ctx_f.value, forecast=fc_f.value, air_quality=aq_f.value,
        stations=st_f.value, travel=tr_f.value, degraded=degraded, now=now,
    )  # fmt: skip
    return Gathered(status="ok", engine_input=inp, degraded=degraded, latency_ms=latency)


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
