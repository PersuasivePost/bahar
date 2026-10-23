"""Overpass API: (a) venue discovery per activity, (b) one batched context query."""

import asyncio
import logging
import math
import re
import time
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

import httpx

from bahar.config import activities as acfg
from bahar.config import osmtags
from bahar.config import sources as cfg
from bahar.data import cache
from bahar.data.geocode import search_nominatim
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

log = logging.getLogger(__name__)

VENUE_IMPACT = "no venues can be proposed"
CONTEXT_IMPACT = (
    "pollution-source exposure (E = 0), green/water microclimate unavailable; lower confidence"
)

_NAME_SPLIT = re.compile(r"[\W_]+")
_REPEATS = re.compile(r"(.)\1+")
_KIND_KEYS = ("leisure", "natural", "tourism", "historic", "boundary", "man_made", "amenity",
              "landuse", "highway")  # fmt: skip
_ROAD_CLASSES = osmtags.ROAD_CLASSES
_INDUSTRY_CLASSES = osmtags.INDUSTRY_CLASSES
_GREEN_VALUES = "|".join(osmtags.GREEN_VALUES)
_FOSSIL = "|".join(osmtags.FOSSIL)


# ------------------------------------------------------------------ transport


def _reject_runtime_error(resp: httpx.Response) -> str | None:
    """Overpass answers 200 with a `remark` when the server timed out or ran out of memory."""
    try:
        remark = resp.json().get("remark", "")
    except ValueError:
        return "response is not JSON"
    return remark[:150] if "runtime error" in remark.lower() else None


_mirrors = list(cfg.OVERPASS_MIRRORS)  # the last mirror that answered is tried first
_bad_until: dict[str, float] = {}  # circuit breaker: mirror -> monotonic time it is trusted again
_failed_at: dict[str, float] = {}
_slots = asyncio.Semaphore(cfg.OVERPASS_MAX_CONCURRENT)


def _ordered_mirrors(mirrors: list[str]) -> list[str]:
    """Healthy mirrors first (last-good first); mirrors marked bad follow, the one that failed
    longest ago first - so with every mirror bad, that one is tried first."""
    now = time.monotonic()
    healthy = [m for m in mirrors if _bad_until.get(m, 0.0) <= now]
    bad = sorted((m for m in mirrors if m not in healthy), key=lambda m: _failed_at.get(m, 0.0))
    return healthy + bad


def _note_attempt(url: str, ok: bool) -> None:
    if ok:
        _bad_until.pop(url, None)
        return
    now = time.monotonic()
    _bad_until[url], _failed_at[url] = now + cfg.OVERPASS_BAD_MIRROR_S, now


async def _run(
    query: str, stage: str, impact: str, attempt_timeout_s: float, deadline_s: float,
    mirrors: list[str] | None = None,
) -> tuple[dict[str, Any] | None, DegradedSource | None]:
    async with _slots:
        resp, failure = await request(
            SourceKey.overpass, stage, impact, "POST", _ordered_mirrors(mirrors or _mirrors),
            data={"data": query}, timeout_s=attempt_timeout_s, deadline_s=deadline_s,
            reject=_reject_runtime_error, on_attempt=_note_attempt,
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
    selectors = acfg.ACTIVITIES[activity].venue_selectors
    clauses = "".join(f"nwr[{sel}]{around};" for sel in selectors)
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
    if "name" not in tags or tags.get("leisure") in acfg.EXCLUDED_LEISURE:
        return None
    if "lat" in el:  # node
        lat, lon = el["lat"], el["lon"]
    elif bounds:  # `out bb` replaces `center` for ways/relations: use the box midpoint
        lat = (bounds["minlat"] + bounds["maxlat"]) / 2
        lon = (bounds["minlon"] + bounds["maxlon"]) / 2
    else:
        return None
    kind = _kind(tags)
    if kind in acfg.NAME_BLOCKLIST_KINDS and acfg.NAME_BLOCKLIST.search(
        f"{tags['name']} {tags.get('name:en', '')}"
    ):
        return None
    access = tags.get("access")
    return Venue(
        id=f"osm:{el['type']}/{el['id']}",
        name=tags.get("name:en") or tags["name"],  # original stays in tags["name"]
        kind=kind,
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
        private=access in acfg.PRIVATE_ACCESS,
    )


def _name_key(name: str) -> str:
    """Comparable form of a venue name: lowercase, no punctuation, generic words (fort, garden,
    park, point, peak, ...) and glued suffixes (Lohgad -> loh) removed, repeated letters
    collapsed. Falls back to the unstripped form when nothing is left ("Dog Park")."""
    words = _NAME_SPLIT.sub(" ", name.casefold()).split()
    core = [w for w in words if w not in acfg.GENERIC_WORDS]
    stripped = []
    for w in core:
        for suffix in acfg.GENERIC_SUFFIXES:
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
        stripped.append(w)
    return _REPEATS.sub(r"\1", "".join(stripped or words))


def _boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """(south, west, north, east) boxes intersect."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _same_place(a: Venue, b: Venue) -> bool:
    """True if two candidates are one real place: close AND similarly named, or a point-mapped
    park lying inside (or within POINT_IN_BBOX_MARGIN_M of) the other's bbox."""
    dist_m = haversine_km(a.lat, a.lon, b.lat, b.lon) * 1000
    wide = a.kind in acfg.WIDE_KINDS or b.kind in acfg.WIDE_KINDS
    limit = acfg.MERGE_DISTANCE_WIDE_M if wide else acfg.MERGE_DISTANCE_M
    # Similar names AND (close OR overlapping boxes: big polygons mapped twice, e.g. a golf
    # course as both the club and the course, have centroids far apart).
    overlap = a.bbox is not None and b.bbox is not None and _boxes_overlap(a.bbox, b.bbox)
    if dist_m <= limit or overlap:
        ratio = SequenceMatcher(None, _name_key(a.name), _name_key(b.name)).ratio()
        if ratio >= acfg.MERGE_NAME_RATIO:
            return True
    for point, area in ((a, b), (b, a)):
        if point.bbox is None and area.bbox is not None and point.kind not in acfg.WIDE_KINDS:
            margin = acfg.POINT_IN_BBOX_MARGIN_M / 111_320
            south, west, north, east = area.bbox
            cos = math.cos(math.radians(point.lat))
            if (
                south - margin <= point.lat <= north + margin
                and west - margin / cos <= point.lon <= east + margin / cos
            ):
                return True
    return False


def _min_area_km2(v: Venue, activity: Activity) -> float:
    if v.kind in acfg.AREA_EXEMPT_KINDS:
        return 0.0
    if v.kind in acfg.PARK_KINDS and acfg.ACTIVITIES[activity].park_area_floor:
        return acfg.PARK_MIN_AREA_KM2
    return acfg.MIN_AREA_KM2


def _good_name(name: str, activity: Activity) -> bool:
    if acfg.BAD_NAME.search(name) or sum(c.isalpha() for c in name) < acfg.MIN_NAME_LETTERS:
        return False
    must = acfg.NAME_MUST_MATCH.get(activity)
    return must is None or must.search(name) is not None


def select_candidates(venues: list[Venue], radius_km: float, activity: Activity) -> list[Venue]:
    """Drop out-of-radius and too-small area features, merge duplicates (the polygon / larger
    area wins; a Latin-script name replaces a non-Latin one), rank by distance and size
    (point-mapped parks rank below polygons), keep MAX_CANDIDATES."""
    pool = [
        v
        for v in venues
        if v.distance_km <= radius_km
        and (v.area_km2 is None or v.area_km2 >= _min_area_km2(v, activity))
        and _good_name(v.name, activity)
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
        size = min(1.0, (v.area_km2 or 0.0) / acfg.RANK_SIZE_SATURATION_KM2)
        point_penalty = (
            acfg.POINT_RANK_PENALTY
            if v.area_km2 is None and v.kind in acfg.PARK_KINDS
            else 0.0
        )
        return v.distance_km / radius_km - acfg.RANK_SIZE_BONUS * size + point_penalty

    return sorted(kept, key=rank)[: acfg.MAX_CANDIDATES]


async def _local(what: str, *, localosm_fn: str, args: tuple[Any, ...]) -> Any:
    """The local OSM index first; None (-> live Overpass) when it is absent, does not cover the
    area, or fails."""
    from bahar.data import localosm  # imports this module: loaded lazily

    if localosm.extract_date() is None:
        return None
    try:
        return await asyncio.to_thread(getattr(localosm, localosm_fn), *args)
    except Exception:
        log.exception("local OSM index failed for %s; using live Overpass", what)
        return None


async def fetch_venues(
    activity: Activity, lat: float, lon: float, radius_km: float
) -> Fetch[list[Venue]]:
    if (local := await _local("venues", localosm_fn="fetch_venues", args=(activity, lat, lon, radius_km))) is not None:
        return local
    key = cache.make_key(
        "venues", activity=activity.value, lat=round(lat, 3), lon=round(lon, 3), r=radius_km
    )
    res = await cache.cached_stale(
        key, cache.STATIC, cfg.STALE_TTL_S, lambda: _fetch_venues(activity, lat, lon, radius_km)
    )
    if res.value is None and res.degraded and res.degraded[0].reason != "parse":
        return await _venues_from_nominatim(activity, lat, lon, radius_km, res.degraded[0])
    return res


async def _fetch_venues(
    activity: Activity, lat: float, lon: float, radius_km: float
) -> Fetch[list[Venue]]:
    started = time.monotonic()
    query = _venue_query(activity, lat, lon, radius_km)
    data, failure = await _run(query, "venues", VENUE_IMPACT, cfg.VENUE_ATTEMPT_TIMEOUT_S, cfg.VENUE_DEADLINE_S)
    if failure:  # one more try on the mirror that answered last, inside the overall budget
        await asyncio.sleep(cfg.VENUE_RETRY_PAUSE_S)
        left = cfg.VENUE_DEADLINE_S - (time.monotonic() - started)
        if left > 1.0:
            data, failure = await _run(query, "venues", VENUE_IMPACT,
                                       min(cfg.VENUE_ATTEMPT_TIMEOUT_S, left), left, _mirrors[:1])  # fmt: skip
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


async def _venues_from_nominatim(
    activity: Activity, lat: float, lon: float, radius_km: float, overpass_failure: DegradedSource
) -> Fetch[list[Venue]]:
    """Overpass failed (not "found nothing"): search Nominatim for the activity's keywords in
    the radius' bounding box. Fewer venues, no tags (opening hours, lit, access). The failure
    stays in `degraded`; no value at all means both services failed."""
    from bahar.data import localosm  # imports this module: loaded lazily

    south, west, north, east = _box(lat, lon, radius_km)
    now = datetime.now(UTC)
    venues: list[Venue] = []
    degraded_list = [overpass_failure]
    for word in acfg.NOMINATIM_KEYWORDS[activity][: cfg.NOMINATIM_VENUE_KEYWORDS_MAX]:
        hits, failure = await search_nominatim(
            word, limit=cfg.NOMINATIM_VENUE_LIMIT, viewbox=(west, north, east, south), bounded=True
        )
        if failure:
            break
        for h in hits:
            if not localosm.matches_first(activity, {h.get("category", ""): h.get("type", "")}):
                continue  # Nominatim's text match also returns other kinds of place
            if (v := _venue_from_hit(h, lat, lon)) is not None:
                venues.append(v)
    chosen = select_candidates(venues, radius_km, activity)
    if not chosen:
        return Fetch(degraded=degraded_list)
    degraded_list.append(degraded(
        SourceKey.nominatim, "venues", "empty",
        "Overpass unavailable: venues found by Nominatim text search instead",
        "fewer venues; no opening hours, lighting or access tags",
    ))  # fmt: skip
    evidence = [
        Evidence(id=f"raw:nominatim:{v.id}", label=f"{v.kind} venue", value=v.name,
                 source=SourceKey.nominatim, fetched_at=now)
        for v in chosen
    ]  # fmt: skip
    return Fetch(value=chosen, degraded=degraded_list, evidence=evidence)


def _box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of the square around the centre."""
    dlat = radius_km / 111.32
    dlon = dlat / math.cos(math.radians(lat))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def _venue_from_hit(hit: dict[str, Any], lat0: float, lon0: float) -> Venue | None:
    name = hit.get("name") or ""
    if not name or hit.get("osm_type") not in ("node", "way", "relation"):
        return None
    south, north, west, east = (float(v) for v in hit["boundingbox"])  # Nominatim order
    lat, lon = float(hit["lat"]), float(hit["lon"])
    bounds = {"minlat": south, "maxlat": north, "minlon": west, "maxlon": east}
    area = _bbox_area_km2(bounds)
    return Venue(
        id=f"osm:{hit['osm_type']}/{hit['osm_id']}", name=name,
        kind=(hit.get("type") if hit.get("type") not in (None, "yes") else hit.get("category", "place")),
        lat=lat, lon=lon, area_km2=area if area > 0 else None,
        bbox=(south, west, north, east) if area > 0 else None,
        distance_km=haversine_km(lat0, lon0, lat, lon), tags={"name": name},
    )  # fmt: skip


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


_classify = osmtags.classify


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
    if (local := await _local("context", localosm_fn="fetch_context", args=(venues,))) is not None:
        return local
    key = cache.make_key("context", ids=sorted(v.id for v in venues))
    return await cache.cached_stale(key, cache.STATIC, cfg.STALE_TTL_S, lambda: _fetch_context(venues))


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
