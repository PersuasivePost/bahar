"""Pure geographic helpers: spherical distance and bearing, a per-query metric frame (UTM),
and shapely operations in metres on the projected OSM context."""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import shapely
from pyproj import Transformer
from shapely import Geometry, LineString, Point, Polygon, STRtree
from shapely.ops import nearest_points

from bahar.models import ContextFeature, LatLon

EARTH_RADIUS_KM = 6371.0088
_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")  # fmt: skip


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Forward azimuth from point 1 to point 2 on the sphere, degrees clockwise from true
    north in [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    x = math.sin(dlmb) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass_word(bearing_deg: float) -> str:
    """16-point compass word (N, NNE, ... NNW) for a bearing."""
    return _COMPASS[round(bearing_deg / 22.5) % 16]


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute angle between two directions, in [0, 180]."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


class LocalFrame:
    """Metric projection for one query: WGS 84 / UTM zone of the anchor longitude. Scale error
    stays < 0.1 % within a zone, far below the heuristics' own uncertainty."""

    def __init__(self, lat: float, lon: float) -> None:
        zone = int((lon + 180.0) // 6.0) % 60 + 1
        epsg = (32600 if lat >= 0 else 32700) + zone
        self._fwd = Transformer.from_crs(4326, epsg, always_xy=True)
        self._inv = Transformer.from_crs(epsg, 4326, always_xy=True)

    def point(self, lat: float, lon: float) -> Point:
        x, y = self._fwd.transform(lon, lat)
        return Point(x, y)

    def coords(self, latlons: Sequence[LatLon]) -> np.ndarray:
        lats, lons = zip(*latlons, strict=True)
        xs, ys = self._fwd.transform(np.array(lons), np.array(lats))
        return np.column_stack([xs, ys])

    def latlon(self, p: Point) -> LatLon:
        lon, lat = self._inv.transform(p.x, p.y)
        return lat, lon


def _polygons(parts: list[np.ndarray]) -> Geometry | None:
    """Closed rings become polygons; open fragments (multipolygon relation members) are merged
    and polygonised. Returns a valid (multi)polygon or None."""
    closed = [Polygon(p) for p in parts if len(p) >= 4 and np.array_equal(p[0], p[-1])]
    fragments = [LineString(p) for p in parts if len(p) >= 2 and not np.array_equal(p[0], p[-1])]
    if fragments:
        merged = shapely.line_merge(shapely.MultiLineString(fragments))
        closed += list(shapely.get_parts(shapely.polygonize(shapely.get_parts(merged))))
    if not closed:
        return None
    geom = shapely.make_valid(shapely.union_all([shapely.make_valid(p) for p in closed]))
    polys = [g for g in shapely.get_parts(geom) if isinstance(g, Polygon) and g.area > 0]
    return shapely.union_all(polys) if polys else None


def _lines(parts: list[np.ndarray]) -> Geometry | None:
    lines = [LineString(p) for p in parts if len(p) >= 2]
    if lines:
        return lines[0] if len(lines) == 1 else shapely.MultiLineString(lines)
    return Point(parts[0][0]) if parts and len(parts[0]) else None


@dataclass(frozen=True)
class ProjectedFeature:
    feature: ContextFeature
    geom: Geometry  # metres, in the query's LocalFrame
    area_km2: float  # 0 for lines and points


def area_km2(geom: Geometry) -> float:
    return float(geom.area) / 1e6


def nearest_point(geom: Geometry, p: Point) -> Point:
    """Point of `geom` closest to `p` (p itself when p lies inside a polygon)."""
    return nearest_points(geom, p)[0]


def covered_share(center: Point, radius_m: float, polygons: Iterable[Geometry]) -> float:
    """Share of the disc (center, radius_m) covered by the union of `polygons`, in [0, 1]."""
    disc = center.buffer(radius_m, quad_segs=16)
    pieces = [shapely.intersection(g, disc) for g in polygons]
    pieces = [g for g in pieces if not g.is_empty]
    if not pieces:
        return 0.0
    return min(1.0, float(shapely.union_all(pieces).area / disc.area))


class FeatureIndex:
    """ContextFeatures projected once per query, with one STRtree per feature group."""

    def __init__(self, frame: LocalFrame, features: Iterable[ContextFeature]) -> None:
        self.frame = frame
        by_group: dict[str, list[ProjectedFeature]] = {}
        for f in features:
            parts = [frame.coords(r) for r in f.rings if r]
            geom = (_polygons(parts) if f.closed else None) or _lines(parts)
            if geom is None or geom.is_empty:
                continue
            pf = ProjectedFeature(f, geom, area_km2(geom) if geom.area > 0 else 0.0)
            by_group.setdefault(f.group, []).append(pf)
        self._items = by_group
        self._trees = {g: STRtree([pf.geom for pf in items]) for g, items in by_group.items()}

    def within(self, group: str, center: Point, radius_m: float) -> list[ProjectedFeature]:
        """Features of `group` whose geometry comes within radius_m of center."""
        tree = self._trees.get(group)
        if tree is None:
            return []
        hits = tree.query(center, predicate="dwithin", distance=radius_m)
        return [self._items[group][i] for i in sorted(hits)]
