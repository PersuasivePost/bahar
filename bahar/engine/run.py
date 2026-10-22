"""The whole engine as one pure function: EngineInput (fetched data + resolved request) ->
EngineResult. No network, no clock reads (`now` is part of the input)."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from bahar.config import weights as w
from bahar.config.activities import ACTIVITIES
from bahar.config.health import DISCLAIMER, resolve_health
from bahar.config.sources import IST
from bahar.engine.access import OpeningHours, parse_opening_hours
from bahar.engine.advice import advise
from bahar.engine.aq import air_quality_assumptions, local_air_quality, station_bias
from bahar.engine.conditions import build_grid
from bahar.engine.evidence import EvidenceStore
from bahar.engine.exposure import hourly_exposure, pollution_sources
from bahar.engine.geo import FeatureIndex, LocalFrame
from bahar.engine.microclimate import Microclimate, microclimate
from bahar.engine.recommend import recommend
from bahar.engine.scoring import ScoringContext, VenueState, score_slot
from bahar.engine.sun import sun_days
from bahar.models import (
    EngineInput,
    EngineResult,
    PollutionSource,
    ScoredSlot,
    SourceKey,
    StationBias,
    Venue,
)

_HOUR = timedelta(hours=1)


class _Static:
    """Per-venue results that do not depend on the hour range: computed once."""

    def __init__(self, inp: EngineInput, store: EvidenceStore, geo_at: datetime) -> None:
        index = FeatureIndex(
            LocalFrame(inp.place.lat, inp.place.lon), inp.context.features if inp.context else []
        )
        self.sources: dict[str, list[PollutionSource]] = {}
        self.micro: dict[str, Microclimate] = {}
        self.bias: dict[str, StationBias | None] = {}
        self.refs: dict[str, str] = {}
        for v in inp.venues:
            self.refs[v.id] = store.place(v.name, f"{v.kind.replace('_', ' ')} venue", SourceKey.overpass, geo_at)
            self.sources[v.id] = pollution_sources(v, index, store, geo_at)
            self.micro[v.id] = microclimate(v, index, store, geo_at)
            cams = inp.air_quality.venues.get(v.id) if inp.air_quality else None
            self.bias[v.id] = station_bias(v, inp.stations, cams, store)


def _states(
    inp: EngineInput, static: _Static, store: EvidenceStore, first: datetime, last: datetime,
    assumptions: list[str],
) -> dict[str, VenueState]:  # fmt: skip
    """VenueStates whose hourly grid covers [first, last]."""
    aq_at = inp.air_quality.fetched_at if inp.air_quality else inp.now
    travel = {t.venue_id: t for t in inp.travel or []}
    out = {}
    for v in inp.venues:
        vw = inp.forecast.venues.get(v.id)
        if vw is None:
            continue
        by_t = {h.t: h for h in vw.hours}
        hours = [h for h in vw.hours if first <= h.t <= last]
        exposure = hourly_exposure(v, static.sources[v.id], hours, store, inp.forecast.fetched_at)
        cams = inp.air_quality.venues.get(v.id) if inp.air_quality else None
        micro = static.micro[v.id]
        aq = local_air_quality(v, exposure, cams, static.bias[v.id], micro.green_share, inp.now, store, aq_at)
        out[v.id] = VenueState(
            venue=v, ref=static.refs[v.id], hours=build_grid(v, hours, exposure, aq, by_t),
            aq_ids={a.t: a.evidence_ids for a in aq}, exposure_ids={e.t: e.evidence_id for e in exposure},
            green_share=micro.green_share, near_water=micro.near_water, micro_ids=micro.evidence_ids,
            travel=travel.get(v.id), opening=_opening(v, assumptions),
        )  # fmt: skip
    return out


def _opening(v: Venue, assumptions: list[str]) -> OpeningHours | None:
    if not v.opening_hours:
        return None
    oh = parse_opening_hours(v.opening_hours)
    if oh is None:
        note = f"opening hours of {v.name} not understood ('{v.opening_hours}'): treated as open"
        if note not in assumptions:
            assumptions.append(note)
    return oh


def _score(ctx: ScoringContext, states: dict[str, VenueState], starts: Sequence[datetime]) -> list[ScoredSlot]:
    return [s for st in states.values() for t in starts if (s := score_slot(ctx, st, t))]


def run_engine(inp: EngineInput) -> EngineResult:
    act = ACTIVITIES[inp.activity]
    health, assumptions = resolve_health(inp.parsed.profile)
    assumptions = [*inp.place.assumptions, *inp.window.assumptions, *assumptions]
    now = inp.now.astimezone(IST)
    starts = inp.window.starts
    if not starts:
        return EngineResult(assumptions=assumptions)

    store = EvidenceStore()
    geo_at = inp.context.fetched_at if inp.context else now
    store.place(inp.place.name, "your location", SourceKey.nominatim, geo_at, user_location=True)
    static = _Static(inp, store, geo_at)
    per_slot = -(-act.duration_min // 60)
    first = starts[0] - _HOUR  # thunder adjacency before the first start
    last = starts[-1] + (per_slot + w.HEAT_WATCH_LOOKAHEAD_H) * _HOUR  # adjacency + heat lookahead
    later_last = starts[-1] + (w.IMPROVE_SEARCH_H + per_slot + 1) * _HOUR
    ctx = ScoringContext(
        activity=act, health=health, radius_km=inp.radius_km,
        sun=sun_days(inp.place.lat, inp.place.lon,
                     {(first + k * _HOUR).date() for k in range(int((later_last - first) / _HOUR) + 1)}),
        store=store, weather_at=inp.forecast.fetched_at,
        aq_at=inp.air_quality.fetched_at if inp.air_quality else now, geo_at=geo_at, now=now,
    )  # fmt: skip

    states = _states(inp, static, store, first, last, assumptions)
    slots = _score(ctx, states, starts)
    later: list[ScoredSlot] = []
    if slots and all(s.blocked for s in slots):
        # Nothing safe in the window: score the following hours to say when it improves.
        states = _states(inp, static, store, first, later_last, assumptions)
        later_starts = [starts[-1] + k * _HOUR for k in range(1, w.IMPROVE_SEARCH_H + 1)]
        later = _score(ctx, states, later_starts)

    rec = recommend(ctx, states, slots, later, inp.window.label, inp.degraded)
    advice = advise(ctx, states[rec.best.venue_id], rec) if rec else None
    if rec is None:
        assumptions.append("no forecast covers the requested time: nothing could be scored")
    assumptions += air_quality_assumptions(inp.stations, list(static.bias.values()), now)
    assumptions.append("travel times by car (OSRM free-flow × time-of-day heuristic; no live traffic)")
    if inp.parsed.commute:
        assumptions.append("commute routing is not supported yet: evaluated around the start point only")

    shown_last = starts[-1] + (per_slot - 1) * _HOUR
    hourly = [hc for st in states.values() for t, hc in sorted(st.hours.items()) if starts[0] <= t <= shown_last]
    return EngineResult(
        recommendation=rec, advice=advice, slots=slots, hourly=hourly, sources=static.sources,
        venue_refs=static.refs, evidence=store.records(), assumptions=list(dict.fromkeys(assumptions)),
        notes=[DISCLAIMER] if health.disclaimer else [],
    )  # fmt: skip
