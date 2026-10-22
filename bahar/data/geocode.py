"""Nominatim geocoding, India-biased, 1 req/s (enforced in data.http)."""

from datetime import UTC, datetime

from bahar.config import sources as cfg
from bahar.data import cache
from bahar.data.http import degraded, request_json
from bahar.data.result import Fetch
from bahar.models import Evidence, Place, SourceKey

IMPACT = "cannot locate the place; query needs clarification"


def search_radius_km(place: Place, activity_radius_km: float) -> float:
    """Whole-city results are centred with at least CITY_MIN_RADIUS_KM; capped at 50 km."""
    radius = activity_radius_km
    if place.is_city:
        radius = max(radius, cfg.CITY_MIN_RADIUS_KM)
    return min(radius, cfg.MAX_RADIUS_KM)


async def geocode(text: str) -> Fetch[Place]:
    key = cache.make_key("geocode", q=" ".join(text.lower().split()))
    return await cache.cached(key, cache.STATIC, lambda: _fetch(text))


async def _fetch(text: str) -> Fetch[Place]:
    data, failure = await request_json(
        SourceKey.nominatim, "geocode", IMPACT, "GET", cfg.NOMINATIM_URL,
        params={"q": text, "format": "jsonv2", "countrycodes": "in", "limit": 5},
    )  # fmt: skip
    if failure:
        return Fetch(degraded=[failure])
    if not data:
        empty = degraded(
            SourceKey.nominatim, "geocode", "empty", f"no match for {text!r} in India", IMPACT
        )
        return Fetch(degraded=[empty])

    # The top hit for a bare town name can be a station or road; prefer an administrative or
    # place result so the city / neighbourhood rank is meaningful.
    hit = next((h for h in data if h.get("category") in ("boundary", "place")), data[0])
    south, north, west, east = (float(v) for v in hit["boundingbox"])  # Nominatim order
    rank = int(hit.get("place_rank", 30))
    place = Place(
        query=text,
        name=hit["display_name"],
        lat=float(hit["lat"]),
        lon=float(hit["lon"]),
        bbox=(south, west, north, east),
        place_rank=rank,
        is_city=rank <= cfg.CITY_MAX_PLACE_RANK,
    )
    evidence = Evidence(
        id="raw:nominatim:place",
        label="Your location",
        value=place.name,
        source=SourceKey.nominatim,
        fetched_at=datetime.now(UTC),
    )
    return Fetch(value=place, evidence=[evidence])
