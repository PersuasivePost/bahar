"""Green and water share within 500 m of a venue, and distance to the nearest mapped water."""

from datetime import datetime
from typing import NamedTuple

import shapely
from shapely import Geometry, LineString, Point, Polygon
from shapely.ops import split

from bahar.config import weights as w
from bahar.config.sources import MICROCLIMATE_RADIUS_M
from bahar.engine.evidence import EvidenceStore
from bahar.engine.geo import FeatureIndex, covered_share
from bahar.models import SourceKey, Venue

_DISC_QUAD_SEGS = 16
_TANGENT_STEP_M = 1.0


class Microclimate(NamedTuple):
    green_share: float
    water_share: float
    water_distance_m: float | None  # None = no water mapped within 500 m
    near_water: bool  # mapped water within NEAR_WATER_M
    evidence_ids: dict[str, str]


def _sea_pieces(coasts: list[Geometry], disc: Polygon) -> list[Polygon]:
    """Parts of the disc on the sea side of the coastline. OSM coastline ways run with land
    on the left and water on the right, so after splitting the disc along the coastline a
    piece is sea when it lies to the right of the nearest coastline segment. A coastline that
    does not cross the disc (one piece) contributes no area."""
    parts = [p for g in coasts for p in shapely.get_parts(g) if isinstance(p, LineString)]
    if not parts:
        return []
    lines = list(shapely.get_parts(shapely.line_merge(shapely.MultiLineString(parts))))
    pieces = [p for p in shapely.get_parts(split(disc, shapely.MultiLineString(lines))) if isinstance(p, Polygon)]
    if len(pieces) < 2:
        return []
    sea = []
    for piece in pieces:
        rp = piece.point_on_surface()
        line = min(lines, key=lambda ln: ln.distance(rp))
        s = line.project(rp)
        a = line.interpolate(max(0.0, s - _TANGENT_STEP_M))
        b = line.interpolate(min(line.length, s + _TANGENT_STEP_M))
        cross = (b.x - a.x) * (rp.y - a.y) - (b.y - a.y) * (rp.x - a.x)
        if cross < 0:  # right of the way direction
            sea.append(piece)
    return sea


def microclimate(
    venue: Venue, index: FeatureIndex, store: EvidenceStore, fetched_at: datetime
) -> Microclimate:
    center: Point = index.frame.point(venue.lat, venue.lon)
    disc = center.buffer(MICROCLIMATE_RADIUS_M, quad_segs=_DISC_QUAD_SEGS)
    green = [pf.geom for pf in index.within("green", center, MICROCLIMATE_RADIUS_M) if pf.area_km2 > 0]
    water = index.within("water", center, MICROCLIMATE_RADIUS_M)
    water_polys = [pf.geom for pf in water if pf.area_km2 > 0]
    coasts = [pf.geom for pf in water if pf.feature.cls == "coastline"]

    green_share = covered_share(center, MICROCLIMATE_RADIUS_M, green)
    water_share = covered_share(center, MICROCLIMATE_RADIUS_M, water_polys + _sea_pieces(coasts, disc))
    water_d = min((float(pf.geom.distance(center)) for pf in water), default=None)

    ids = {
        "green_share": store.value(
            f"green cover within {MICROCLIMATE_RADIUS_M} m of {venue.name}", round(green_share * 100),
            "%", SourceKey.overpass, fetched_at, note="share of the area mapped as park/forest/grass",
        ),
        "water_share": store.value(
            f"water within {MICROCLIMATE_RADIUS_M} m of {venue.name}", round(water_share * 100),
            "%", SourceKey.overpass, fetched_at, note="lakes, wetlands, rivers and sea",
        ),
    }  # fmt: skip
    if water_d is not None:
        ids["water_distance"] = store.value(
            f"distance from {venue.name} to the nearest water", round(water_d, -1), "m",
            SourceKey.overpass, fetched_at,
            note=f"near water (≤ {w.NEAR_WATER_M:g} m)" if water_d <= w.NEAR_WATER_M else None,
        )  # fmt: skip
    near = water_d is not None and water_d <= w.NEAR_WATER_M
    return Microclimate(round(green_share, 3), round(water_share, 3), water_d, near, ids)
