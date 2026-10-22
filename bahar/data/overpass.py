"""Overpass API: (a) venue discovery per activity, (b) one batched context query."""

import math
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

import httpx

from bahar.config import sources as cfg
from bahar.config import venues as vcfg
from bahar.data import cache
from bahar.data.http import degraded, request
from bahar.data.result import Fetch
from bahar.engine.geo import haversine_km
from bahar.models import (
    Activity,
    ContextData,
    ContextFeature,
    DegradedSource,
    Evidence,
    LatLon,
    SourceKey,
    Venue,
)

VENUE_IMPACT = "no venues can be proposed"
CONTEXT_IMPACT = (
    "pollution-source exposure (E = 0), green/water microclimate unavailable; lower confidence"
)

_NAME_SPLIT = re.compile(r"[\W_]+")
_REPEATS = re.compile(r"(.)\1+")
_KIND_KEYS = ("leisure", "natural", "tourism", "historic", "boundary", "man_made", "amenity",
              "landuse", "highway")  # fmt: skip
_ROAD_CLASSES = ("motorway", "trunk", "primary", "secondary")
_INDUSTRY_CLASSES = ("industrial", "landfill", "construction", "quarry")
_GREEN_VALUES = ("forest|grass|meadow|village_green|recreation_ground|park|garden|"
                 "nature_reserve|wood|scrub|grassland")  # fmt: skip
_FOSSIL = "coal|gas|oil|diesel|lignite|petroleum"


# ------------------------------------------------------------------ transport


def _reject_runtime_error(resp: httpx.Response) -> str | None:
    """Overpass answers 200 with a `remark` when the server timed out or ran out of memory."""
    try:
        remark = resp.json().get("remark", "")
    except ValueError:
        return "response is not JSON"
    return remark[:150] if "runtime error" in remark.lower() else None


_mirrors = list(cfg.OVERPASS_MIRRORS)  # the last mirror that answered is tried first


async def _run(
    query: str, stage: str, impact: str, attempt_timeout_s: float, deadline_s: float
) -> tuple[dict[str, Any] | None, DegradedSource | None]:
    resp, failure = await request(
        SourceKey.overpass, stage, impact, "POST", _mirrors,
        data={"data": query}, timeout_s=attempt_timeout_s, deadline_s=deadline_s,
        reject=_reject_runtime_error,
    )  # fmt: skip
    if resp is None:
        return None, failure
    answered = next((m for m in _mirrors if m.startswith(str(resp.url).split("?")[0])), None)
    if answered:
        _mirrors.remove(answered)
        _mirrors.insert(0, answered)
    return resp.json(), None


# ------------------------------------------------------------------ venues


def _venue_query(activity: Activity, lat: float, lon: float, radius_km: float) -> str:
    around = f"(around:{int(radius_km * 1000)},{lat},{lon})"
    # No `[name]` filter here: it makes Overpass's planner 20x slower on rare tags. Unnamed
    # features are dropped in _parse_venue instead.
    clauses = "".join(f"nwr[{sel}]{around};" for sel in vcfg.VENUE_SELECTORS[activity])
    return f"[out:json][timeout:{cfg.OVERPASS_SERVER_TIMEOUT_S}];({clauses});out tags bb;"


def _kind(tags: dict[str, str]) -> str:
    for key in _KIND_KEYS:
        if key in tags:
            return tags[key] if tags[key] != "yes" else key
    return "place"


def _bbox_area_km2(bounds: dict[str, float]) -> float:
    """Bounding-box area: an upper bound on the true area (Overpass `out bb` gives no polygon
    area without fetching full geometry)."""
    mid_lat = math.radians((bounds["minlat"] + bounds["maxlat"]) / 2)
    height = (bounds["maxlat"] - bounds["minlat"]) * 111.32
    width = (bounds["maxlon"] - bounds["minlon"]) * 111.32 * math.cos(mid_lat)
    return height * width


def _parse_venue(el: dict[str, Any], lat0: float, lon0: float) -> Venue | None:
    tags: dict[str, str] = el.get("tags", {})
    bounds = el.get("bounds")
    if "name" not in tags or tags.get("leisure") in vcfg.EXCLUDED_LEISURE:
        return None
    if "lat" in el:  # node
        lat, lon = el["lat"], el["lon"]
    elif bounds:  # `out bb` replaces `center` for ways/relations: use the box midpoint
        lat = (bounds["minlat"] + bounds["maxlat"]) / 2
        lon = (bounds["minlon"] + bounds["maxlon"]) / 2
    else:
        return None
    access = tags.get("access")
    return Venue(
        id=f"osm:{el['type']}/{el['id']}",
        name=tags.get("name:en") or tags["name"],  # original stays in tags["name"]
        kind=_kind(tags),
        lat=lat,
        lon=lon,
        area_km2=_bbox_area_km2(bounds) if bounds else None,
        bbox=(bounds["minlat"], bounds["minlon"], bounds["maxlat"], bounds["maxlon"])
        if bounds
        else None,
        distance_km=haversine_km(lat0, lon0, lat, lon),
        tags=tags,
        opening_hours=tags.get("opening_hours"),
        access=access,
        lit={"yes": True, "no": False}.get(tags.get("lit", "")),
        private=access in vcfg.PRIVATE_ACCESS,
    )


def _name_key(name: str) -> str:
    """Comparable form of a venue name: lowercase, no punctuation, generic words (fort, garden,
    park, point, peak, ...) and glued suffixes (Lohgad -> loh) removed, repeated letters
    collapsed. Falls back to the unstripped form when nothing is left ("Dog Park")."""
    words = _NAME_SPLIT.sub(" ", name.casefold()).split()
    core = [w for w in words if w not in vcfg.GENERIC_WORDS]
    stripped = []
    for w in core:
        for suffix in vcfg.GENERIC_SUFFIXES:
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
        stripped.append(w)
    return _REPEATS.sub(r"\1", "".join(stripped or words))


def _same_place(a: Venue, b: Venue) -> bool:
    """True if two candidates are one real place: close AND similarly named, or a point-mapped
    park lying inside (or within POINT_IN_BBOX_MARGIN_M of) the other's bbox."""
    dist_m = haversine_km(a.lat, a.lon, b.lat, b.lon) * 1000
    wide = a.kind in vcfg.WIDE_KINDS or b.kind in vcfg.WIDE_KINDS
    limit = vcfg.MERGE_DISTANCE_WIDE_M if wide else vcfg.MERGE_DISTANCE_M
    if dist_m <= limit:
        ratio = SequenceMatcher(None, _name_key(a.name), _name_key(b.name)).ratio()
        if ratio >= vcfg.MERGE_NAME_RATIO:
            return True
    for point, area in ((a, b), (b, a)):
        if point.bbox is None and area.bbox is not None and point.kind not in vcfg.WIDE_KINDS:
            margin = vcfg.POINT_IN_BBOX_MARGIN_M / 111_320
            south, west, north, east = area.bbox
            cos = math.cos(math.radians(point.lat))
            if (
                south - margin <= point.lat <= north + margin
                and west - margin / cos <= point.lon <= east + margin / cos
            ):
                return True
    return False


def _min_area_km2(v: Venue, activity: Activity) -> float:
    if v.kind in vcfg.AREA_EXEMPT_KINDS:
        return 0.0
    if v.kind in vcfg.PARK_KINDS and activity in vcfg.PARK_ACTIVITIES:
        return vcfg.PARK_MIN_AREA_KM2
    return vcfg.MIN_AREA_KM2


def select_candidates(venues: list[Venue], radius_km: float, activity: Activity) -> list[Venue]:
    """Drop out-of-radius and too-small area features, merge duplicates (the polygon / larger
    area wins; a Latin-script name replaces a non-Latin one), rank by distance and size
    (point-mapped parks rank below polygons), keep MAX_CANDIDATES."""
    pool = [
        v
        for v in venues
        if v.distance_km <= radius_km
        and (v.area_km2 is None or v.area_km2 >= _min_area_km2(v, activity))
    ]
    pool.sort(key=lambda v: -(v.area_km2 if v.area_km2 is not None else -1.0))  # polygons first
    kept: list[Venue] = []
    for v in pool:
        twin = next((i for i, k in enumerate(kept) if _same_place(k, v)), None)
        if twin is None:
            kept.append(v)
        elif not kept[twin].name.isascii() and v.name.isascii():
            kept[twin] = kept[twin].model_copy(update={"name": v.name})

    def rank(v: Venue) -> float:
        size = min(1.0, (v.area_km2 or 0.0) / vcfg.RANK_SIZE_SATURATION_KM2)
        point_penalty = (
            vcfg.POINT_RANK_PENALTY
            if v.area_km2 is None and v.kind in vcfg.PARK_KINDS
            else 0.0
        )
        return v.distance_km / radius_km - vcfg.RANK_SIZE_BONUS * size + point_penalty

    return sorted(kept, key=rank)[: vcfg.MAX_CANDIDATES]


async def fetch_venues(
    activity: Activity, lat: float, lon: float, radius_km: float
) -> Fetch[list[Venue]]:
    key = cache.make_key(
        "venues", activity=activity.value, lat=round(lat, 3), lon=round(lon, 3), r=radius_km
    )
    return await cache.cached(key, cache.STATIC, lambda: _fetch_venues(activity, lat, lon, radius_km))


async def _fetch_venues(
    activity: Activity, lat: float, lon: float, radius_km: float
) -> Fetch[list[Venue]]:
    data, failure = await _run(
        _venue_query(activity, lat, lon, radius_km), "venues", VENUE_IMPACT,
        cfg.VENUE_ATTEMPT_TIMEOUT_S, cfg.VENUE_DEADLINE_S,
    )  # fmt: skip
    if failure:
        return Fetch(degraded=[failure])
    try:
        parsed = (_parse_venue(el, lat, lon) for el in data["elements"])  # type: ignore[index]
        chosen = select_candidates([v for v in parsed if v], radius_km, activity)
    except (KeyError, TypeError, ValueError) as exc:
        bad = degraded(SourceKey.overpass, "venues", "parse", repr(exc)[:200], VENUE_IMPACT)
        return Fetch(degraded=[bad])

    now = datetime.now(UTC)
    evidence = [
        Evidence(
            id=f"raw:overpass:{v.id}", label=f"{v.kind} venue", value=v.name,
            source=SourceKey.overpass, fetched_at=now,
        )
        for v in chosen
    ]  # fmt: skip
    return Fetch(value=chosen, evidence=evidence)


# ------------------------------------------------------------------ context


def _square(lat: float, lon: float, radius_m: float) -> str:
    """Overpass bbox `south,west,north,east` of a square of half-side radius_m."""
    dlat = radius_m / 111_320
    dlon = dlat / math.cos(math.radians(lat))
    return f"{lat - dlat:.5f},{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f}"


def _context_query(venues: list[Venue]) -> str:
    """One union query. Bbox squares instead of `around` circles: Overpass answers them 4-5x
    faster on regex tag filters. Features in the square corners lie slightly beyond the
    radius; the engine applies the true circular distance."""
    clauses: list[str] = []
    for v in venues:
        far = _square(v.lat, v.lon, cfg.CONTEXT_RADIUS_M)
        near = _square(v.lat, v.lon, cfg.MICROCLIMATE_RADIUS_M)
        clauses += [
            f'way[highway~"^({"|".join(_ROAD_CLASSES)})$"]({far});',
            f'nwr[landuse~"^({"|".join(_INDUSTRY_CLASSES)})$"]({far});',
            f'nwr[power=plant]["plant:source"~"{_FOSSIL}"]({far});',
            f'nwr[~"^(landuse|leisure|natural)$"~"^({_GREEN_VALUES})$"]({near});',
            f'nwr[natural~"^(water|wetland|coastline)$"]({near});',
            f'way[waterway~"^(river|canal)$"]({near});',
        ]
    # Clip returned geometry to the overall area so huge relations (national parks, the
    # coastline) do not bloat the payload.
    pad = cfg.CONTEXT_RADIUS_M / 111_320
    south = min(v.lat for v in venues) - pad
    north = max(v.lat for v in venues) + pad
    west = min(v.lon for v in venues) - pad / math.cos(math.radians(south))
    east = max(v.lon for v in venues) + pad / math.cos(math.radians(north))
    return (
        f"[out:json][timeout:{cfg.OVERPASS_SERVER_TIMEOUT_S}];({''.join(clauses)});"
        f"out geom({south:.5f},{west:.5f},{north:.5f},{east:.5f}) qt;"
    )


def _classify(tags: dict[str, str]) -> tuple[str, str]:
    """(group, cls) for the first matching rule; anything the query returned that matches no
    other rule matched the green regex."""
    if tags.get("highway") in _ROAD_CLASSES:
        return "road", tags["highway"]
    if tags.get("landuse") in _INDUSTRY_CLASSES:
        return "industry", tags["landuse"]
    if tags.get("power") == "plant":
        return "power", "power_plant"
    if tags.get("natural") == "coastline":
        return "water", "coastline"
    if tags.get("natural") in ("water", "wetland"):
        return "water", "water"
    if tags.get("waterway") in ("river", "canal"):
        return "water", "river"
    return "green", "green"


def _points(geometry: list[dict[str, float] | None]) -> list[LatLon]:
    return [(p["lat"], p["lon"]) for p in geometry if p]


def _rings(el: dict[str, Any]) -> list[list[LatLon]]:
    if el["type"] == "node":
        return [[(el["lat"], el["lon"])]]
    if el["type"] == "way":
        return [_points(el.get("geometry", []))]
    return [  # relation: outer members only (multipolygon boundaries)
        _points(m.get("geometry", []))
        for m in el.get("members", [])
        if m.get("type") == "way" and m.get("role") == "outer"
    ]


def _parse_feature(el: dict[str, Any]) -> ContextFeature | None:
    tags: dict[str, str] = el.get("tags", {})
    group, cls = _classify(tags)
    rings = [r for r in _rings(el) if r]
    if not rings:
        return None
    is_line = group == "road" or cls in ("coastline", "river")
    is_polygon = not is_line and (el["type"] == "relation" or rings[0][0] == rings[0][-1])
    return ContextFeature(
        osm_id=f"{el['type']}/{el['id']}",
        group=group,  # type: ignore[arg-type]
        cls=cls,
        name=tags.get("name"),
        ref=tags.get("ref"),
        closed=is_polygon and len(rings[0]) > 3,
        rings=rings,
    )


async def fetch_context(venues: list[Venue]) -> Fetch[ContextData]:
    if not venues:
        return Fetch(value=ContextData(fetched_at=datetime.now(UTC), features=[]))
    key = cache.make_key("context", ids=sorted(v.id for v in venues))
    return await cache.cached(key, cache.STATIC, lambda: _fetch_context(venues))


async def _fetch_context(venues: list[Venue]) -> Fetch[ContextData]:
    data, failure = await _run(
        _context_query(venues), "context", CONTEXT_IMPACT,
        cfg.CONTEXT_ATTEMPT_TIMEOUT_S, cfg.CONTEXT_DEADLINE_S,
    )  # fmt: skip
    if failure:
        return Fetch(degraded=[failure])
    try:
        features = [f for el in data["elements"] if (f := _parse_feature(el))]  # type: ignore[index]
    except (KeyError, TypeError, ValueError) as exc:
        bad = degraded(SourceKey.overpass, "context", "parse", repr(exc)[:200], CONTEXT_IMPACT)
        return Fetch(degraded=[bad])
    return Fetch(value=ContextData(fetched_at=datetime.now(UTC), features=features))
