"""From scored slots to a Recommendation: best slot, alternative venue, alternative time,
one why-not, avoid ranges, status (ok | no_safe_window) and confidence."""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from bahar.config import weights as w
from bahar.engine.scoring import ScoringContext, VenueState, hour_blocks
from bahar.models import (
    AvoidRange,
    DegradedSource,
    Factor,
    ReasonCode,
    Recommendation,
    ScoredSlot,
    WhyNot,
)

_HOUR = timedelta(hours=1)


def raw_score(s: ScoredSlot) -> float:
    """100 + all deltas, unclamped: keeps ordering among slots whose score clamps at 0."""
    return 100.0 + sum(f.delta for f in s.factors)


def _safe_key(s: ScoredSlot, distance: Mapping[str, float]) -> tuple[float, datetime, float]:
    return (-raw_score(s), s.start, distance[s.venue_id])


def _least_bad_key(s: ScoredSlot) -> tuple[int, float, datetime]:
    return (len(s.gates), -raw_score(s), s.start)


def rank(slots: Iterable[ScoredSlot], distance: Mapping[str, float]) -> list[ScoredSlot]:
    """Safe slots by score (ties: earlier, then nearer), then blocked ones least-bad first."""
    slots = list(slots)
    safe = sorted((s for s in slots if not s.blocked), key=lambda s: _safe_key(s, distance))
    blocked = sorted((s for s in slots if s.blocked), key=_least_bad_key)
    return safe + blocked


def _gate_priority(f: Factor) -> int:
    return w.GATE_PRIORITY.index(f.code.value) if f.code.value in w.GATE_PRIORITY else len(w.GATE_PRIORITY)


def decisive_gate(slot: ScoredSlot) -> Factor:
    return min(slot.gates, key=_gate_priority)


def _alt_time(best: ScoredSlot, ranked: Sequence[ScoredSlot]) -> ScoredSlot | None:
    """Best other start at the same venue, preferring one that does not overlap the best
    session (at least ALT_TIME_MIN_GAP_H apart)."""
    same = [s for s in ranked if s.venue_id == best.venue_id and s.start != best.start
            and s.blocked == best.blocked]  # fmt: skip
    gap = timedelta(hours=w.ALT_TIME_MIN_GAP_H)
    apart = [s for s in same if abs(s.start - best.start) >= max(gap, best.end - best.start)]
    return (apart or same or [None])[0]


def _decisive(loser: ScoredSlot, best: ScoredSlot, loser_state: VenueState) -> Factor:
    """The factor where the losing slot falls furthest behind the best one."""
    if loser.blocked:
        return decisive_gate(loser)
    totals: dict[str, float] = defaultdict(float)
    for f in best.factors:
        totals[f.code] -= f.delta
    for f in loser.factors:
        totals[f.code] += f.delta
    if not totals:  # identical scores: lost the tie-break (later start or further away)
        return Factor(code=ReasonCode.travel_time, label="equal score, but further away or later", delta=0)
    code = min(totals, key=totals.__getitem__)
    mine = [f for f in loser.factors if f.code == code]
    if mine:
        return min(mine, key=lambda f: f.delta)
    theirs = max((f for f in best.factors if f.code == code), key=lambda f: f.delta)
    if code == ReasonCode.green_cooling:
        return Factor(code=code, label=f"less green cover ({loser_state.green_share:.0%} within 500 m)",
                      delta=-theirs.delta, evidence_ids=[loser_state.micro_ids["green_share"]])  # fmt: skip
    return Factor(code=theirs.code, label=f"lacks: {theirs.label}", delta=-theirs.delta,
                  evidence_ids=theirs.evidence_ids)  # fmt: skip


def _why_not(
    best: ScoredSlot, alt_venue: ScoredSlot | None, ranked: Sequence[ScoredSlot],
    states: Mapping[str, VenueState],
) -> WhyNot | None:  # fmt: skip
    """The venue nearest the user that is neither the pick nor the alternative, with its best
    slot and the factor that decided against it."""
    taken = {best.venue_id, alt_venue.venue_id if alt_venue else None}
    others = sorted({s.venue_id for s in ranked} - taken, key=lambda vid: states[vid].venue.distance_km)
    if not others:
        return None
    slot = next(s for s in ranked if s.venue_id == others[0])
    decisive = _decisive(slot, best, states[slot.venue_id])
    return WhyNot(venue_id=slot.venue_id, slot=slot, decisive=decisive,
                  close=_is_close_call(best, slot, decisive, states[slot.venue_id]))  # fmt: skip


def _is_close_call(best: ScoredSlot, slot: ScoredSlot, decisive: Factor, state: VenueState) -> bool:
    """Not a real loss: scores within the configured gap (neither blocked), or the decisive
    factor is air quality and the PM2.5 means differ by less than the loser's own uncertainty
    band (mean of its hourly low-high half-widths over the slot)."""
    if slot.blocked or best.blocked:
        return False
    if best.score - slot.score < w.WHY_NOT_CLOSE_SCORE_GAP:
        return True
    if decisive.code != ReasonCode.air_quality or best.pm25_mean is None or slot.pm25_mean is None:
        return False
    hours = [h for t, h in state.hours.items() if slot.start <= t < slot.end and h.pm25_low is not None
             and h.pm25_high is not None]  # fmt: skip
    if not hours:
        return False
    half_band = sum((h.pm25_high - h.pm25_low) / 2 for h in hours) / len(hours)  # type: ignore[operator]
    return abs(slot.pm25_mean - best.pm25_mean) <= half_band


def avoid_ranges(ctx: ScoringContext, st: VenueState, starts: Sequence[datetime]) -> list[AvoidRange]:
    """Blocked start hours of the requested window at the venue, adjacent hours with the same
    dominant (highest-priority) reason merged; the MAX_AVOID_RANGES longest, in time order."""
    out: list[AvoidRange] = []
    for t in sorted(set(starts)):
        hc = st.hours.get(t)
        blocks = hour_blocks(ctx, st, hc) if hc else []
        if not blocks:
            continue
        top = min(blocks, key=_gate_priority)
        prev = out[-1] if out else None
        if prev and prev.end == t and prev.reason == top.code:
            prev.end = t + _HOUR
            prev.evidence_ids = list(dict.fromkeys([*prev.evidence_ids, *top.evidence_ids]))
        else:
            out.append(AvoidRange(venue_id=st.venue.id, start=t, end=t + _HOUR, reason=top.code,
                                  evidence_ids=top.evidence_ids))  # fmt: skip
    longest = sorted(out, key=lambda r: (-(r.end - r.start), r.start))[: w.MAX_AVOID_RANGES]
    return sorted(longest, key=lambda r: r.start)


def confidence(degraded: Iterable[DegradedSource], travel_estimated: bool) -> float:
    stages = {d.stage for d in degraded}
    if travel_estimated:
        stages.add("routing")
    value = 1.0 - sum(w.CONFIDENCE_PENALTY.get(s, 0.0) for s in stages)
    return round(max(w.CONFIDENCE_FLOOR, value), 2)


def recommend(
    ctx: ScoringContext,
    states: Mapping[str, VenueState],
    slots: Sequence[ScoredSlot],
    later: Sequence[ScoredSlot],
    window_label: str,
    degraded: Sequence[DegradedSource],
) -> Recommendation | None:
    """`slots`: the requested window; `later`: starts after it, scored only when nothing in
    the window is safe (to say when conditions improve)."""
    if not slots:
        return None
    distance = {vid: st.venue.distance_km for vid, st in states.items()}
    ranked = rank(slots, distance)
    best = ranked[0]
    status = "no_safe_window" if best.blocked else "ok"
    alt_venue = next((s for s in ranked if s.venue_id != best.venue_id and s.blocked == best.blocked), None)

    improves_at = None
    if best.blocked:
        safe_later = [s for s in later if not s.blocked]
        improves_at = min((s.start for s in safe_later), default=None)

    st = states[best.venue_id]
    return Recommendation(
        status=status, best=best, alt_venue=alt_venue, alt_time=_alt_time(best, ranked),
        why_not=_why_not(best, alt_venue, ranked, states),
        avoid=avoid_ranges(ctx, st, [s.start for s in slots]),  # in start-hour terms
        improves_at=improves_at,
        window_label=window_label,
        confidence=confidence(degraded, bool(st.travel and st.travel.estimated)),
    )  # fmt: skip
