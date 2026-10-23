"""Nominatim geocoding, India-biased, 1 req/s (enforced in data.http)."""

import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from bahar.config import lexicon as lx
from bahar.config import sources as cfg
from bahar.data import cache
from bahar.data.http import degraded, request_json
from bahar.data.result import Fetch
from bahar.engine.geo import haversine_km
from bahar.models import DegradedSource, Evidence, Place, SourceKey

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


async def search_nominatim(
    text: str, *, limit: int = cfg.GEOCODE_CANDIDATES,
    viewbox: tuple[float, float, float, float] | None = None, bounded: bool = False,
) -> tuple[list[dict[str, Any]], DegradedSource | None]:
    """One Nominatim search in India; `viewbox` is (west, north, east, south)."""
    params: dict[str, Any] = {"q": text, "format": "jsonv2", "countrycodes": "in", "limit": limit}
    if viewbox:
        params |= {"viewbox": ",".join(f"{v:.5f}" for v in viewbox), "bounded": int(bounded)}
    data, failure = await request_json(
        SourceKey.nominatim, "geocode", IMPACT, "GET", cfg.NOMINATIM_URL, params=params
    )
    return sorted(data or [], key=_rank, reverse=True), failure


async def _search(text: str) -> tuple[list[dict[str, Any]], DegradedSource | None]:
    return await search_nominatim(text)


async def _fetch(text: str) -> Fetch[Place]:
    ranked, failure = await _search(text)
    if failure:
        return Fetch(degraded=[failure])
    token = text.split(",")[0].strip()
    notes: list[str] = []
    if _weak(token, ranked):
        for variant in _spelling_variants(token)[: cfg.GEOCODE_MAX_VARIANTS]:
            alt, alt_failure = await _search(text.replace(token, variant, 1))
            if alt_failure or not alt:
                continue
            if not ranked or _name_match(token, alt[0]) > _name_match(token, ranked[0]):
                ranked, notes = alt, [f"searched '{token}' as '{variant}' (spelling variant)"]
            if not _weak(token, ranked):
                break
    if _weak(token, ranked):
        rungs, notes_ladder = await _ladder(text)
        if rungs:
            ranked, notes = rungs, notes_ladder
    if not ranked:
        empty = degraded(
            SourceKey.nominatim, "geocode", "empty", f"no match for {text!r} in India", IMPACT
        )
        return Fetch(degraded=[empty])

    hit = ranked[0]
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
        assumptions=[*notes, *_runner_up_note(hit, ranked[1:])],
    )
    evidence = Evidence(
        id="raw:nominatim:place",
        label="Your location",
        value=place.name,
        source=SourceKey.nominatim,
        fetched_at=datetime.now(UTC),
    )
    return Fetch(value=place, evidence=[evidence])


def _weak(token: str, ranked: list[dict[str, Any]]) -> bool:
    return not ranked or _name_match(token, ranked[0]) < cfg.GEOCODE_NAME_MATCH


def _city_of(text: str) -> tuple[str, str] | None:
    """(place head, city) from "head, City", "head City" or "City head"; None if no city."""
    pieces = [p.strip() for p in text.split(",") if p.strip()]
    if len(pieces) > 1:
        return ", ".join(pieces[:-1]), pieces[-1]
    if m := lx.CITY.search(text):
        head = f"{text[: m.start()]} {text[m.end() :]}".strip()
        return (head, lx.CITIES[m.group().lower()]) if head else None
    words = text.split()
    return (" ".join(words[1:]), words[0]) if len(words) > 1 else None


async def _ladder(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The full text found nothing usable: (a) the place + its city, (b) the place without
    trailing landmark words, (c) the city alone. Returns the first rung that works with an
    assumption saying what was used."""
    split = _city_of(text)
    if split is None:
        return [], []
    head, city = split
    stripped = _strip_landmark(head)
    rungs = [(f"{head}, {city}", head, f"located '{head}' via {city}")]
    if stripped != head:
        rungs.append((f"{stripped}, {city}", stripped, f"centred on {stripped}; {head} not found"))
    rungs.append((city, city, f"centred on {city}; {head} not found"))
    tried = {" ".join(text.lower().replace(",", " ").split())}
    for query, name, note in rungs:
        key = " ".join(query.lower().replace(",", " ").split())
        if key in tried:
            continue
        tried.add(key)
        hits, failure = await _search(query)
        if not failure and hits and _name_match(name, hits[0]) >= cfg.GEOCODE_NAME_MATCH:
            return hits, [note]
    return [], []


def _strip_landmark(head: str) -> str:
    out = head
    for word in cfg.GEOCODE_LANDMARK_WORDS:
        out = re.sub(rf"\s+{re.escape(word)}$", "", out, flags=re.I)
    return out.strip() or head


def agrees(place: Place, latin: str) -> bool:
    """Does a result found by its Devanagari name match the parser's Latin spelling?"""
    hit = {"display_name": place.name}
    return _name_match(latin.split(",")[0].strip(), hit) >= cfg.GEOCODE_NATIVE_AGREE


def _name_match(token: str, hit: dict[str, Any]) -> float:
    """1.0 when the query's place word is inside the result name ("Whitefield (Kadugodi)"),
    else their difflib ratio."""
    name = (hit.get("name") or hit["display_name"].split(", ")[0]).casefold()
    token = token.casefold()
    return 1.0 if token in name else SequenceMatcher(None, token, name).ratio()


def _spelling_variants(token: str) -> list[str]:
    """Alias spelling first (Gurgaon <-> Gurugram), then swapped vowel pairs ("Powia" ->
    "Powai"), vowel substitutions (Hinjewadi / Hinjawadi), other adjacent-letter swaps."""
    chars = list(token)
    pairs = [(i, sum(c.lower() in "aeiou" for c in chars[i : i + 2])) for i in range(len(chars) - 1)
             if chars[i].isalpha() and chars[i + 1].isalpha() and chars[i].lower() != chars[i + 1].lower()]  # fmt: skip

    def swap(i: int) -> str:
        t = chars.copy()
        t[i], t[i + 1] = t[i + 1], t[i]
        return "".join(t)

    alias = cfg.GEOCODE_ALIASES.get(token.lower())
    out = [alias] if alias else []
    out += [swap(i) for i, v in pairs if v == 2]
    out += _vowel_variants(token)
    out += [swap(i) for i, v in pairs if v < 2]
    return list(dict.fromkeys(v for v in out if v.lower() != token.lower()))


def _vowel_variants(token: str) -> list[str]:
    """Each single vowel substitution, in GEOCODE_VOWEL_SWAPS order, left to right."""
    lower = token.lower()
    out: list[str] = []
    for old, new in cfg.GEOCODE_VOWEL_SWAPS:
        for m in re.finditer(re.escape(old), lower):
            variant = token[: m.start()] + new + token[m.end() :]
            if variant.lower() != lower and variant not in out:
                out.append(variant)
    return out


def _rank(hit: dict[str, Any]) -> tuple[float, int]:
    """Nominatim importance first (2 decimals), then address type: suburb / city above
    village / hamlet, everything else (stations, landuse, roads) in between."""
    kind = hit.get("addresstype") or hit.get("type")
    tier = 1
    if kind in cfg.GEOCODE_PREFERRED_TYPES:
        tier = 2
    elif kind in cfg.GEOCODE_MINOR_TYPES:
        tier = 0
    return round(float(hit.get("importance") or 0.0), 2), tier


def _runner_up_note(hit: dict[str, Any], rest: list[dict[str, Any]]) -> list[str]:
    """Name the best other result when it is almost as prominent and somewhere else."""
    lat, lon = float(hit["lat"]), float(hit["lon"])
    for other in rest:
        km = haversine_km(lat, lon, float(other["lat"]), float(other["lon"]))
        if km < cfg.GEOCODE_DISTINCT_KM:
            continue  # the same place mapped twice (landuse + locality)
        gap = float(hit.get("importance") or 0.0) - float(other.get("importance") or 0.0)
        if gap <= cfg.GEOCODE_CLOSE_IMPORTANCE:
            return [f"location read as {_short(hit)}; also possible: {_short(other)}"]
        return []
    return []


def _short(hit: dict[str, Any]) -> str:
    """Name, district, state: "Andheri, Sangrur, Punjab"."""
    parts = [p for p in hit["display_name"].split(", ") if not p.isdigit() and p != "India"]
    return ", ".join([parts[0], *parts[1:][-2:]])
