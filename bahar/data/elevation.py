"""OpenTopoData SRTM 30 m elevation lookup (hikes), <= 100 points per request, 1 req/s."""

from collections.abc import Sequence

from bahar.config import sources as cfg
from bahar.data import cache
from bahar.data.http import degraded, request_json
from bahar.data.result import Fetch
from bahar.models import SourceKey

IMPACT = "no elevation profile for the hike (ascent / terrain difficulty unknown)"


async def fetch_elevation(points: Sequence[tuple[float, float]]) -> Fetch[list[float | None]]:
    key = cache.make_key("elevation", pts=[(round(la, 5), round(lo, 5)) for la, lo in points])
    return await cache.cached(key, cache.STATIC, lambda: _fetch(points))


async def _fetch(points: Sequence[tuple[float, float]]) -> Fetch[list[float | None]]:
    elevations: list[float | None] = []
    size = cfg.OPENTOPODATA_MAX_POINTS
    for start in range(0, len(points), size):
        chunk = points[start : start + size]
        data, failure = await request_json(
            SourceKey.opentopodata, "elevation", IMPACT, "GET", cfg.OPENTOPODATA_URL,
            params={"locations": "|".join(f"{la:.5f},{lo:.5f}" for la, lo in chunk)},
        )  # fmt: skip
        if failure:
            return Fetch(degraded=[failure])
        try:
            elevations += [r["elevation"] for r in data["results"]]
        except (KeyError, TypeError):
            bad = degraded(SourceKey.opentopodata, "elevation", "parse", "unexpected response", IMPACT)
            return Fetch(degraded=[bad])
    return Fetch(value=elevations)
