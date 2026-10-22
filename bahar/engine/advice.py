"""Deterministic advice for the recommended slot (CLAUDE.md "Advice rules"): what to carry,
what to watch for, when to avoid. The SLM only phrases these; every item cites evidence."""

from datetime import timedelta
from itertools import pairwise

from bahar.config import weights as w
from bahar.engine.conditions import cite
from bahar.engine.geo import angle_diff_deg, compass_word
from bahar.engine.scoring import ScoringContext, VenueState, slot_hours
from bahar.engine.sun import overlap_min
from bahar.engine.traffic import travel_multiplier
from bahar.models import (
    Advice,
    AdviceItem,
    HeatFlag,
    HourConditions,
    ReasonCode,
    Recommendation,
    SourceKey,
)

_HOUR = timedelta(hours=1)
_HOT_FLAGS = (HeatFlag.high, HeatFlag.very_high, HeatFlag.extreme)


def advise(ctx: ScoringContext, st: VenueState, rec: Recommendation) -> Advice:
    slot = rec.best
    hs = slot_hours(ctx, st, slot.start) or []
    if not hs:
        return Advice(avoid_when=rec.avoid)
    v, store, act, extras = st.venue, ctx.store, ctx.activity, ctx.health.extras

    def c(hc: HourConditions, field: str) -> str:
        return cite(store, v, hc, field, ctx.weather_at)

    hottest = max(hs, key=lambda h: h.wbgt)
    carry: list[AdviceItem] = []
    if hottest.wbgt >= w.ADVICE_WATER_WBGT or act.duration_min >= w.ADVICE_WATER_DURATION_MIN or "hydration" in extras:
        carry.append(AdviceItem(code="water", label="water", evidence_ids=[c(hottest, "wbgt")]))
    if hottest.wbgt >= w.ADVICE_ELECTROLYTES_WBGT:
        carry.append(AdviceItem(code="electrolytes", label="electrolytes / ORS", evidence_ids=[c(hottest, "wbgt")]))
    sunniest = max(hs, key=lambda h: h.uv)
    if sunniest.uv >= min(w.ADVICE_SUNSCREEN_UV, ctx.health.uv_threshold):
        carry.append(AdviceItem(code="sunscreen_cap", label="sunscreen and a cap", evidence_ids=[c(sunniest, "uv")]))
    likeliest = max(hs, key=lambda h: h.rain_prob)
    if likeliest.rain_prob >= w.ADVICE_RAIN_GEAR_PROB[0]:
        carry.append(AdviceItem(code="rain_gear", label="rain jacket or umbrella", evidence_ids=[c(likeliest, "rain_prob")]))
    if "inhaler" in extras:
        carry.append(AdviceItem(code="inhaler", label="your reliever inhaler"))
    sensitive = ctx.health.key != "default"
    peak_e = max(hs, key=lambda h: h.exposure)
    if slot.pm25_mean is not None and slot.pm25_mean >= w.ADVICE_N95_PM25 and sensitive:
        aq_ids = [f.evidence_ids[0] for f in slot.factors if f.code is ReasonCode.air_quality and f.evidence_ids]
        carry.append(AdviceItem(code="n95", label="an N95 mask", evidence_ids=aq_ids))
    elif peak_e.exposure >= w.ADVICE_N95_EXPOSURE:
        carry.append(AdviceItem(code="n95", label="an N95 mask (busy roads upwind)",
                                evidence_ids=[st.exposure_ids[peak_e.t], *peak_e.top_source_ids[:1]]))  # fmt: skip
    sun = ctx.sun.get(slot.start.date())
    if sun and slot.end > sun.sunset:
        sid = store.value(f"sunset on {sun.day:%a %d %b}", f"{sun.sunset:%H:%M}", "IST", SourceKey.astral, ctx.now)
        carry.append(AdviceItem(code="light_gear", label="a light / reflective gear", evidence_ids=[sid]))
    if sun and (st.near_water or st.green_share >= w.ADVICE_GREEN_SHARE):
        dusk = (sun.sunset - timedelta(minutes=w.ADVICE_DUSK_WINDOW_MIN),
                sun.sunset + timedelta(minutes=w.ADVICE_DUSK_WINDOW_MIN))  # fmt: skip
        if overlap_min((slot.start, slot.end), dusk) > 0:
            ids = [st.micro_ids[k] for k in ("water_distance", "green_share") if k in st.micro_ids]
            carry.append(AdviceItem(code="insect_repellent", label="insect repellent (dusk near water / greenery)",
                                    evidence_ids=ids))  # fmt: skip
    if act.key == "hiking" and hs[0].rain_48h >= w.SLIPPERY_TRAIL_48H_MM:
        carry.append(AdviceItem(code="grip_shoes", label="shoes with good grip", evidence_ids=[c(hs[0], "rain_48h")]))

    return Advice(carry=carry, watch_for=_watch_for(ctx, st, rec, hs), avoid_when=rec.avoid)


def _watch_for(
    ctx: ScoringContext, st: VenueState, rec: Recommendation, hs: list[HourConditions]
) -> list[AdviceItem]:
    slot, v, store, extras = rec.best, st.venue, ctx.store, ctx.health.extras
    out: list[AdviceItem] = []

    def c(hc: HourConditions, field: str) -> str:
        return cite(store, v, hc, field, ctx.weather_at)

    # Wind shift: within the session and the hour after it.
    seq = [*hs, st.hours.get(hs[-1].t + _HOUR)]
    for a, b in pairwise(seq):
        if (b is not None and b.wind_kmh >= w.WIND_SHIFT_MIN_KMH
                and angle_diff_deg(a.wind_dir_deg, b.wind_dir_deg) >= w.WIND_SHIFT_DEG):  # fmt: skip
            label = (f"wind turns from {compass_word(a.wind_dir_deg)} to {compass_word(b.wind_dir_deg)} "
                     f"around {b.t:%H:%M}")  # fmt: skip
            ids = [c(a, "wind_dir_deg"), c(b, "wind_dir_deg"), st.exposure_ids.get(b.t, "")]
            out.append(AdviceItem(code="wind_shift", label=label, evidence_ids=[i for i in ids if i]))
            break
    # Heat rising: the first hour after the start that reaches the "high" flag.
    if hs[0].heat_flag not in _HOT_FLAGS:
        t = hs[0].t + _HOUR
        while t <= slot.end + w.HEAT_WATCH_LOOKAHEAD_H * _HOUR:
            hc = st.hours.get(t)
            if hc and hc.heat_flag in _HOT_FLAGS:
                out.append(AdviceItem(code="heat_rising", label=f"heat stress rises from {t:%H:%M}",
                                      evidence_ids=[c(hc, "wbgt")]))  # fmt: skip
                break
            t += _HOUR
    for f in slot.factors:
        if f.code is ReasonCode.thunderstorm:
            out.append(AdviceItem(code="storm_risk", label=f.label, evidence_ids=f.evidence_ids))
        elif f.code is ReasonCode.wet_ground:
            out.append(AdviceItem(code="wet_ground", label=f.label, evidence_ids=f.evidence_ids))
        elif f.code is ReasonCode.rush_hour:
            out.append(AdviceItem(code="rush_hour", label="rush-hour traffic on the way there",
                                  evidence_ids=f.evidence_ids))  # fmt: skip
    back = travel_multiplier(slot.end)
    if st.travel is not None and back >= w.TRAVEL_PEAK:
        mid = store.value(f"traffic multiplier at {slot.end:%a %H:%M}", back, "×", SourceKey.engine, ctx.now,
                          note="time-of-day heuristic on OSRM free-flow times; no live traffic")  # fmt: skip
        out.append(AdviceItem(code="rush_hour", label="rush-hour traffic on the way back", evidence_ids=[mid]))
    sun = ctx.sun.get(slot.start.date())
    if sun and slot.end > sun.sunset:
        sid = store.value(f"sunset on {sun.day:%a %d %b}", f"{sun.sunset:%H:%M}", "IST", SourceKey.astral, ctx.now)
        out.append(AdviceItem(code="lit_paths", label="stick to lit paths after sunset", evidence_ids=[sid]))
    if "warm_up" in extras:
        out.append(AdviceItem(code="warm_up", label="warm up gradually; stop if breathing gets hard"))
    if "shorter_sessions" in extras:
        out.append(AdviceItem(code="shorter_sessions", label="keep it shorter, with rest breaks"))
    hottest = max(hs, key=lambda h: h.wbgt)
    if "avoid_high_intensity_in_heat" in extras and hottest.wbgt >= w.HEAT_INTENSITY_WBGT:
        out.append(AdviceItem(code="ease_off_in_heat", label="keep the effort easy in the heat",
                              evidence_ids=[c(hottest, "wbgt")]))  # fmt: skip
    return out
