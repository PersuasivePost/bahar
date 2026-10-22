"""OSRM table service: free-flow travel time from one origin to all candidates."""

from collections.abc import Sequence
from typing import Literal

from bahar.config import sources as cfg
from bahar.data import cache
from bahar.data.http import request_json
from bahar.data.result import Fetch
from bahar.engine.geo import haversine_km
from bahar.models import DegradedSource, SourceKey, TravelTime, Venue

Mode = Literal["car", "bike", "foot"]
IMPACT = "travel times are straight-line estimates (haversine x detour / mean speed)"


def _estimate(origin: tuple[float, float], venue: Venue, mode: Mode) -> TravelTime:
    km = haversine_km(*origin, venue.lat, venue.lon) * cfg.HAVERSINE_DETOUR
    return TravelTime(
        venue_id=venue.id, minutes=km / cfg.FALLBACK_SPEED_KMH[mode] * 60, estimated=True
    )


async def fetch_travel(
    origin: tuple[float, float], venues: Sequence[Venue], mode: Mode = "car"
) -> Fetch[list[TravelTime]]:
    key = cache.make_key(
        "route", o=[round(c, 4) for c in origin], mode=mode,
        to=[(v.id, round(v.lat, 4), round(v.lon, 4)) for v in venues],
    )  # fmt: skip
    return await cache.cached(key, cache.STATIC, lambda: _fetch(origin, venues, mode))


async def _fetch(
    origin: tuple[float, float], venues: Sequence[Venue], mode: Mode
) -> Fetch[list[TravelTime]]:
    coords = ";".join([f"{origin[1]:.5f},{origin[0]:.5f}"] + [f"{v.lon:.5f},{v.lat:.5f}" for v in venues])
    data, failure = await request_json(
        SourceKey.osrm, "routing", IMPACT, "GET", cfg.OSRM_URLS[mode] + coords,
        params={
            "sources": "0",
            "destinations": ";".join(str(i) for i in range(1, len(venues) + 1)),
            "annotations": "duration",
        },
        timeout_s=cfg.ROUTING_TIMEOUT_S,
    )  # fmt: skip
    durations: list[float | None] = []
    if not failure:
        try:
            durations = data["durations"][0]
            if data.get("code") != "Ok" or len(durations) != len(venues):
                raise ValueError(f"code={data.get('code')}, {len(durations)} durations")
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            failure = DegradedSource(
                source=SourceKey.osrm, stage="routing", reason="parse", detail=repr(exc)[:200],
                impact=IMPACT,
            )  # fmt: skip
    if failure:
        durations = [None] * len(venues)

    times = [
        TravelTime(venue_id=v.id, minutes=d / 60, estimated=False)
        if d is not None
        else _estimate(origin, v, mode)
        for v, d in zip(venues, durations, strict=True)
    ]
    unrouted = [t for t in times if t.estimated]
    if unrouted and not failure:  # OSRM answered but some pairs are unroutable (null)
        failure = DegradedSource(
            source=SourceKey.osrm, stage="routing", reason="empty",
            detail=f"{len(unrouted)} venues unroutable", impact=IMPACT,
        )  # fmt: skip
    return Fetch(value=times, degraded=[failure] if failure else [])
