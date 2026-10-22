"""Wind-aware pollution-source exposure index E(h) per venue (CLAUDE.md "Exposure index").

E_raw(h) = sum over the top source groups of w * a * k * s * f, E = tanh(E_raw):
  a  upwind alignment  exp(-delta^2 / (2 * 30^2)), delta = angle(bearing to source, wind-from)
  k  distance decay    exp(-d / 400 m)
  s  dilution          clamp(3 / (u + 2), 0.4, 1.0)
  a, s blend toward the stagnant values (0.5, 1.3) as u drops from 2 to 0.5 m/s
  f  hourly activity   road traffic / construction work hours / 1
"""

import math
from collections.abc import Sequence
from datetime import datetime

from bahar.config import weights as w
from bahar.config.sources import CONTEXT_RADIUS_M
from bahar.engine.evidence import EvidenceStore
from bahar.engine.geo import (
    FeatureIndex,
    ProjectedFeature,
    angle_diff_deg,
    compass_word,
    haversine_km,
    initial_bearing_deg,
    nearest_point,
)
from bahar.engine.traffic import road_traffic_factor
from bahar.models import (
    HourExposure,
    PollutionSource,
    SourceContribution,
    SourceKey,
    Venue,
    WeatherHour,
)

_KMH_PER_MS = 3.6


def _weight(cls: str, members: Sequence[ProjectedFeature]) -> float:
    base = w.SOURCE_WEIGHTS[cls]
    if cls in w.AREA_SCALED_SOURCES:
        # 0.9 x min(1, sqrt(area)): a group's area is the sum of its polygons.
        return base * min(1.0, math.sqrt(sum(m.area_km2 for m in members)))
    return base


def describe(source: PollutionSource) -> str:
    """'Western Express Highway (motorway), 420 m NE' — the cited form of a source."""
    label = source.cls.replace("_", " ")
    head = f"{source.name} ({label})" if source.name else f"unnamed {label}"
    if source.distance_m < w.INSIDE_SOURCE_M:
        return f"{head}, at the venue"
    return f"{head}, {source.distance_m:.0f} m {compass_word(source.bearing_deg)}"


def pollution_sources(
    venue: Venue, index: FeatureIndex, store: EvidenceStore, fetched_at: datetime
) -> list[PollutionSource]:
    """Source groups within 2 km of the venue centroid, grouped by (class, name or ref) so a
    road split into many OSM ways counts once; unnamed features group by class + OSM id.
    Same-class groups within SOURCE_MERGE_M of a higher-ranked one are merged (one corridor),
    then the EXPOSURE_TOP_GROUPS with the highest w * exp(-d / 400) are kept."""
    center = index.frame.point(venue.lat, venue.lon)
    groups: dict[tuple[str, str], list[ProjectedFeature]] = {}
    for group in w.SOURCE_GROUPS:
        for pf in index.within(group, center, CONTEXT_RADIUS_M):
            f = pf.feature
            groups.setdefault((f.cls, f.name or f.ref or f.osm_id), []).append(pf)

    ranked: list[tuple[float, PollutionSource]] = []
    for (cls, _), members in groups.items():
        weight = _weight(cls, members)
        if weight <= 0:
            continue
        nearest = min(members, key=lambda m: m.geom.distance(center))
        d = float(nearest.geom.distance(center))
        lat, lon = index.frame.latlon(nearest_point(nearest.geom, center))
        name = next((m.feature.name or m.feature.ref for m in members if m.feature.name or m.feature.ref), None)
        src = PollutionSource(
            cls=cls, name=name, weight=round(weight, 3), distance_m=round(d, 1),
            bearing_deg=round(initial_bearing_deg(venue.lat, venue.lon, lat, lon), 1),
            lat=lat, lon=lon, evidence_id="",
        )  # fmt: skip
        ranked.append((weight * math.exp(-d / w.DISTANCE_DECAY_M), src))
    ranked.sort(key=lambda t: -t[0])

    deduped: list[PollutionSource] = []
    for _, src in ranked:
        twin = next((i for i, k in enumerate(deduped) if _same_corridor(k, src)), None)
        if twin is None:
            deduped.append(src)
        else:
            deduped[twin] = _merge(deduped[twin], src)

    kept = []
    for src in deduped[: w.EXPOSURE_TOP_GROUPS]:
        note = f"bearing {src.bearing_deg:.0f}° from the venue centroid"
        if src.aliases:
            note += f"; also mapped as {', '.join(src.aliases)}"
        eid = store.value(
            f"pollution source near {venue.name}", describe(src), None, SourceKey.overpass,
            fetched_at, note=note,
        )  # fmt: skip
        kept.append(src.model_copy(update={"evidence_id": eid}))
    return kept


def _same_corridor(a: PollutionSource, b: PollutionSource) -> bool:
    """Same class and nearest points within SOURCE_MERGE_M: separately mapped carriageways,
    unnamed slip roads or twin names (JVLR / Adi Sankaracharya Marg) of one corridor."""
    return a.cls == b.cls and haversine_km(a.lat, a.lon, b.lat, b.lon) * 1000 <= w.SOURCE_MERGE_M


def _merge(kept: PollutionSource, other: PollutionSource) -> PollutionSource:
    """`kept` ranks higher by w * exp(-d/400); the higher-weight group survives (ties: kept),
    and the other's different name becomes an alias."""
    survivor, dropped = (other, kept) if other.weight > kept.weight else (kept, other)
    aliases = [*kept.aliases, *other.aliases]
    if dropped.name and dropped.name != survivor.name:
        aliases.append(dropped.name)
    return survivor.model_copy(update={"aliases": list(dict.fromkeys(aliases))})


def _alignment_dilution(src: PollutionSource, wind_dir: float | None, u_ms: float | None) -> tuple[float, float]:
    """(a, s), blended smoothly between directional and stagnant air:
    blend = clamp((u - 0.5) / 1.5, 0, 1). Missing wind -> blend 0 (direction unknown)."""
    if wind_dir is None or u_ms is None:
        return (w.INSIDE_ALIGNMENT if src.distance_m < w.INSIDE_SOURCE_M else w.STAGNANT_ALIGNMENT,
                w.STAGNATION_FACTOR)  # fmt: skip
    blend = min(1.0, max(0.0, (u_ms - w.CALM_BLEND_U0_MS) / w.CALM_BLEND_SPAN_MS))
    delta = angle_diff_deg(src.bearing_deg, wind_dir)
    a_dir = math.exp(-(delta**2) / (2 * w.UPWIND_SIGMA_DEG**2))
    lo, hi = w.DILUTION_CLAMP
    s_dir = min(hi, max(lo, w.DILUTION_K / (u_ms + w.DILUTION_U0_MS)))
    a = blend * a_dir + (1 - blend) * w.STAGNANT_ALIGNMENT
    s = blend * s_dir + (1 - blend) * w.STAGNATION_FACTOR
    if src.distance_m < w.INSIDE_SOURCE_M:
        a = w.INSIDE_ALIGNMENT
    return a, s


def _activity_factor(cls: str, t: datetime) -> float:
    """Hourly emission activity: road traffic, construction work hours, otherwise 1."""
    if cls in w.ROAD_SOURCES:
        return road_traffic_factor(t)
    if cls == "construction":
        return 1.0 if t.hour in w.CONSTRUCTION_ACTIVE_HOURS else w.CONSTRUCTION_IDLE_FACTOR
    return 1.0


def hourly_exposure(
    venue: Venue,
    sources: Sequence[PollutionSource],
    hours: Sequence[WeatherHour],
    store: EvidenceStore,
    fetched_at: datetime,
) -> list[HourExposure]:
    """E(h) for each given forecast hour, with up to 3 cited contributors."""
    out = []
    for h in hours:
        u_ms = h.wind_kmh / _KMH_PER_MS if h.wind_kmh is not None else None
        terms = []
        for i, src in enumerate(sources):
            a, s = _alignment_dilution(src, h.wind_dir_deg, u_ms)
            k = math.exp(-src.distance_m / w.DISTANCE_DECAY_M)
            terms.append((src.weight * a * k * s * _activity_factor(src.cls, h.t), i))
        exposure = math.tanh(sum(t for t, _ in terms))
        terms.sort(reverse=True)
        top = [
            SourceContribution(source=i, term=round(t, 4), evidence_id=sources[i].evidence_id)
            for t, i in terms[: w.EXPOSURE_TOP_CONTRIBUTORS]
            if t >= w.EXPOSURE_MIN_CONTRIBUTION
        ]
        eid = store.value(
            f"pollution exposure index at {venue.name}, {h.t:%a %H:%M}", round(exposure, 2),
            None, SourceKey.engine, fetched_at,
            note="modelled estimate; 0 = no upwind sources, approaching 1 = strong upwind sources",
        )  # fmt: skip
        out.append(HourExposure(venue_id=venue.id, t=h.t, exposure=round(exposure, 3), top=top, evidence_id=eid))
    return out
