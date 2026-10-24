"""Slot scoring: a slot is (venue, start hour) covering the activity duration.

Hard gates first (thunderstorm, rain, wind, darkness, AQ limit, WBGT limit, closed, outside
radius), then a soft score 0-100 = 100 - penalties + bonuses. Every gate and factor carries
a reason code, a short label, its delta and the evidence ids it used. Gated slots still get
their soft score so the least-bad one can be chosen when nothing is safe.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from bahar.config import weights as w
from bahar.config.activities import ActivityProfile
from bahar.config.health import HealthProfile
from bahar.engine.access import OpeningHours, is_open
from bahar.engine.aq import MODELLED
from bahar.engine.conditions import cite, cite_time
from bahar.engine.dose import dose_share_pct, session_dose_ug
from bahar.engine.evidence import EvidenceStore
from bahar.engine.sun import SunDay, overlap_min
from bahar.engine.traffic import travel_multiplier
from bahar.models import (
    Factor,
    HourConditions,
    ReasonCode,
    ScoredSlot,
    SourceKey,
    TravelTime,
    Venue,
)

_HOUR = timedelta(hours=1)
R = ReasonCode


@dataclass(frozen=True)
class VenueState:
    """Everything known about one candidate venue, ready for scoring."""

    venue: Venue
    ref: str  # v# evidence id of the venue name
    hours: dict[datetime, HourConditions]
    aq_ids: dict[datetime, dict[str, str]]  # hour -> HourAirQuality.evidence_ids
    exposure_ids: dict[datetime, str]
    green_share: float
    near_water: bool
    micro_ids: dict[str, str]
    travel: TravelTime | None
    opening: OpeningHours | None


@dataclass(frozen=True)
class ScoringContext:
    activity: ActivityProfile
    health: HealthProfile
    radius_km: float
    sun: dict[date, SunDay]  # at the search centre (< 2 min apart across 50 km)
    store: EvidenceStore
    weather_at: datetime  # fetched_at of each source, for the evidence records
    aq_at: datetime
    geo_at: datetime
    now: datetime

    @property
    def high_intensity(self) -> bool:
        return self.activity.intensity == "high"

    @property
    def pm25_limit(self) -> float:
        return self.health.pm25_limit_high if self.high_intensity else self.health.pm25_limit

    @property
    def wbgt_stop(self) -> float:
        return self.health.wbgt_stop_high if self.high_intensity else self.health.wbgt_stop

    @property
    def hours_per_slot(self) -> int:
        return math.ceil(self.activity.duration_min / 60)


# ---------------------------------------------------------------- shared predicates


def thunder_blocks(ctx: ScoringContext, hc: HourConditions) -> bool:
    return hc.weather_code in w.THUNDER_CODES and ctx.activity.key in w.THUNDER_BLOCKED


def rain_blocks(ctx: ScoringContext, rain_mm: float) -> bool:
    if rain_mm >= w.RAIN_BLOCK_MM:
        return True
    return rain_mm >= w.RAIN_BLOCK_LIGHT_MM and ctx.activity.key in w.RAIN_LIGHT_BLOCKED


def wind_blocks(ctx: ScoringContext, wind: float, gust: float) -> bool:
    return wind > ctx.activity.wind_max_kmh or gust > ctx.activity.gust_max_kmh


def light_limits(ctx: ScoringContext, venue: Venue, day: date) -> tuple[datetime, datetime] | None:
    """(earliest start, latest end) allowed by the activity's light rule; None = any time."""
    sun = ctx.sun.get(day)
    rule = ctx.activity.light
    if sun is None or rule == "golden_bonus" or (rule == "daylight_or_lit" and venue.lit):
        return None
    if rule == "dusk_ok":
        return (sun.sunrise - timedelta(minutes=w.DAWN_GRACE_MIN),
                sun.sunset + timedelta(minutes=w.DUSK_GRACE_MIN))  # fmt: skip
    if rule == "evening_ok":
        return sun.sunrise, datetime.combine(day, time(w.EVENING_OK_END_HOUR), sun.sunset.tzinfo)
    return sun.sunrise, sun.sunset  # daylight, daylight_or_lit (unlit), back_before_sunset


def darkness_gates(ctx: ScoringContext, venue: Venue) -> bool:
    """False when darkness beyond the dusk grace is only a soft penalty: dusk_ok activities
    (running, walking, cycling) at urban venues not mapped lit=no."""
    if ctx.activity.light != "dusk_ok":
        return True
    if venue.lit is False:
        return True
    return venue.kind in w.NON_URBAN_KINDS and venue.lit is not True


def aq_penalty(ctx: ScoringContext, pm25: float) -> float:
    """0 at AQ_PENALTY_FLOOR, AQ_PENALTY_AT_LIMIT at the profile limit, same slope beyond."""
    slope = w.AQ_PENALTY_AT_LIMIT / (ctx.pm25_limit - w.AQ_PENALTY_FLOOR)
    return max(0.0, pm25 - w.AQ_PENALTY_FLOOR) * slope


def _ramp(x: float, lo: float, hi: float) -> float:
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


# ---------------------------------------------------------------- evidence helpers


def _sun_ids(ctx: ScoringContext, day: date) -> list[str]:
    sun = ctx.sun.get(day)
    if sun is None:
        return []
    return [
        ctx.store.value(f"{what} on {day:%a %d %b}", f"{t:%H:%M}", "IST", SourceKey.astral, ctx.now)
        for what, t in (("sunrise", sun.sunrise), ("sunset", sun.sunset))
    ]


def _limit_id(ctx: ScoringContext, label: str, value: float, unit: str, note: str) -> str:
    return ctx.store.value(label, value, unit, SourceKey.engine, ctx.now, note=note)


def _span(start: datetime, end: datetime) -> str:
    return f"{start:%a %H:%M}–{end:%H:%M}"


# ---------------------------------------------------------------- slot scoring


def slot_hours(ctx: ScoringContext, st: VenueState, start: datetime) -> list[HourConditions] | None:
    hs = [st.hours.get(start + k * _HOUR) for k in range(ctx.hours_per_slot)]
    return None if any(h is None for h in hs) else hs  # type: ignore[return-value]


def score_slot(ctx: ScoringContext, st: VenueState, start: datetime) -> ScoredSlot | None:
    """Score one slot; None when the forecast does not cover it."""
    hs = slot_hours(ctx, st, start)
    if hs is None:
        return None
    act, v, store = ctx.activity, st.venue, ctx.store
    end = start + timedelta(minutes=act.duration_min)
    gates: list[Factor] = []
    factors: list[Factor] = []

    def c(hc: HourConditions, field: str) -> str:
        return cite(store, v, hc, field, ctx.weather_at)

    # -- outside radius
    if v.distance_km > ctx.radius_km:
        eid = store.value(f"distance from your location to {v.name}", round(v.distance_km, 1),
                          "km", SourceKey.overpass, ctx.geo_at)  # fmt: skip
        gates.append(Factor(code=R.outside_radius, label="outside the search radius", delta=0,
                            gate=True, evidence_ids=[st.ref, eid]))  # fmt: skip

    # -- thunderstorm (in or adjacent to the slot) / storm risk
    around = [st.hours.get(start - _HOUR), *hs, st.hours.get(start + len(hs) * _HOUR)]
    thunder = next((h for h in around if h and h.weather_code in w.THUNDER_CODES), None)
    if thunder:
        ids = [c(thunder, "weather_code"), cite_time(store, v, thunder.t, "thunderstorm", ctx.weather_at)]
        if act.key in w.THUNDER_BLOCKED:
            gates.append(Factor(code=R.thunderstorm, label=f"thunderstorm forecast around {thunder.t:%H:%M}",
                                delta=0, gate=True, evidence_ids=ids))  # fmt: skip
        else:
            factors.append(Factor(code=R.thunderstorm, label=f"thunderstorm forecast around {thunder.t:%H:%M}",
                                  delta=-w.THUNDER_PENALTY, evidence_ids=ids))  # fmt: skip
    else:
        risky = next((h for h in hs if h.cape >= w.CAPE_WARN_JKG and h.rain_prob >= w.CAPE_WARN_RAIN_PROB), None)
        if risky:
            factors.append(Factor(code=R.thunderstorm, label="thunderstorm risk (unstable air, rain likely)",
                                  delta=-w.CAPE_PENALTY, evidence_ids=[c(risky, "cape"), c(risky, "rain_prob")]))  # fmt: skip

    # -- rain
    wettest = max(hs, key=lambda h: h.rain_mm)
    if rain_blocks(ctx, wettest.rain_mm):
        gates.append(Factor(code=R.rain, label=f"{wettest.rain_mm:.1f} mm/h rain forecast", delta=0,
                            gate=True, evidence_ids=[c(wettest, "rain_mm")]))  # fmt: skip
    likeliest = max(hs, key=lambda h: h.rain_prob)
    if likeliest.rain_prob > act.rain_prob_max:
        over = likeliest.rain_prob - act.rain_prob_max
        pen = min(w.RAIN_PROB_PENALTY_MAX, w.RAIN_PROB_PENALTY_BASE + w.RAIN_PROB_PENALTY_PER_PCT * over)
        factors.append(Factor(code=R.rain, label=f"rain chance {likeliest.rain_prob:.0f}% (limit {act.rain_prob_max:.0f}%)",
                              delta=-round(pen, 1), evidence_ids=[c(likeliest, "rain_prob")]))  # fmt: skip

    # -- wind
    windiest = max(hs, key=lambda h: max(h.wind_kmh / act.wind_max_kmh, h.gust_kmh / act.gust_max_kmh))
    wind_ids = [c(windiest, "wind_kmh"), c(windiest, "gust_kmh")]
    wind_label = f"wind {windiest.wind_kmh:.0f} km/h, gusts {windiest.gust_kmh:.0f} km/h"
    if wind_blocks(ctx, windiest.wind_kmh, windiest.gust_kmh):
        gates.append(Factor(code=R.wind, label=wind_label + " (too strong)", delta=0, gate=True,
                            evidence_ids=wind_ids))  # fmt: skip
    elif (windiest.wind_kmh > w.WIND_PENALTY_SHARE * act.wind_max_kmh
          or windiest.gust_kmh > w.WIND_PENALTY_SHARE * act.gust_max_kmh):  # fmt: skip
        factors.append(Factor(code=R.wind, label=wind_label + " (near the limit)", delta=-w.WIND_PENALTY,
                              evidence_ids=wind_ids))  # fmt: skip

    # -- darkness
    limits = light_limits(ctx, v, start.date())
    if limits and (start < limits[0] or end > limits[1]):
        sun_ids = _sun_ids(ctx, start.date())
        if darkness_gates(ctx, v):
            gates.append(Factor(code=R.darkness, label="outside daylight for this activity", delta=0,
                                gate=True, evidence_ids=sun_ids))  # fmt: skip
        else:
            lit = v.lit is True
            pen = w.DARK_PENALTY_LIT if lit else w.DARK_PENALTY_UNLIT_UNKNOWN
            lit_id = store.value(f"lighting at {v.name}", "lit" if lit else "not mapped", None,
                                 SourceKey.overpass, ctx.geo_at, note="OSM lit tag")  # fmt: skip
            factors.append(Factor(code=R.darkness, label="after dark" + (" (lit venue)" if lit else " (lighting unknown)"),
                                  delta=-pen, evidence_ids=[*sun_ids, st.ref, lit_id]))  # fmt: skip

    # -- opening hours
    if st.opening and ctx.sun.get(start.date()) and not is_open(st.opening, start, end, ctx.sun[start.date()]):
        eid = store.value(f"opening hours of {v.name}", st.opening.raw, None, SourceKey.overpass, ctx.geo_at)
        gates.append(Factor(code=R.closed, label="closed at this time", delta=0, gate=True,
                            evidence_ids=[st.ref, eid]))  # fmt: skip

    # -- air quality: session mean of local PM2.5 vs the profile limit; band in evidence
    pm25_mean = None
    dose_pct = None
    pms = [h.pm25_local for h in hs]
    if all(p is not None for p in pms):
        pm25_mean = sum(pms) / len(pms)  # type: ignore[arg-type]
        low = sum(h.pm25_low for h in hs) / len(hs)  # type: ignore[misc]
        high = sum(h.pm25_high for h in hs) / len(hs)  # type: ignore[misc]
        when = f"{v.name}, {_span(start, end)}"
        mean_id = store.value(f"PM2.5 session mean at {when}", round(pm25_mean, 1), "µg/m³",
                              SourceKey.engine, ctx.aq_at,
                              note=f"band {low:.0f}–{high:.0f} µg/m³; {MODELLED}")  # fmt: skip
        band_ids = [
            store.value(f"PM2.5 session mean at {when}, {end_name} end", round(x, 1), "µg/m³",
                        SourceKey.engine, ctx.aq_at, note=MODELLED)
            for end_name, x in (("low", low), ("high", high))
        ]  # fmt: skip
        hourly_ids = [st.aq_ids[h.t]["pm25_local"] for h in hs if "pm25_local" in st.aq_ids.get(h.t, {})]
        limit_id = _limit_id(ctx, f"PM2.5 limit for this profile ({act.intensity} intensity)",
                             ctx.pm25_limit, "µg/m³", "Bahar health profile, from CPCB NAQI bands")  # fmt: skip
        if pm25_mean > ctx.pm25_limit:
            gates.append(Factor(code=R.air_quality, label=f"PM2.5 ≈ {pm25_mean:.0f} µg/m³ above the {ctx.pm25_limit:.0f} limit",
                                delta=0, gate=True, evidence_ids=[mean_id, limit_id, *band_ids]))  # fmt: skip
        pen = aq_penalty(ctx, pm25_mean)
        if pen > 0:
            factors.append(Factor(code=R.air_quality, label=f"PM2.5 ≈ {pm25_mean:.0f} µg/m³ (session mean)",
                                  delta=-round(pen, 1), evidence_ids=[mean_id, *band_ids, *hourly_ids]))  # fmt: skip
        dose = session_dose_ug(pm25_mean, act.ventilation_lpm, act.duration_min)
        dose_pct = dose_share_pct(dose)
        dose_id = store.value(f"inhaled PM2.5 this session at {when}", round(dose_pct), "%",
                              SourceKey.engine, ctx.aq_at,
                              note=f"≈ {dose:.0f} µg; share of a full day breathing 'satisfactory' "
                                   f"air (60 µg/m³) at rest; {MODELLED}")  # fmt: skip
        pen = min(w.DOSE_PENALTY_MAX, w.DOSE_PENALTY_PER_PCT * dose_pct)
        if pen >= 0.5:
            factors.append(Factor(code=R.inhaled_dose, label=f"inhaled dose ≈ {dose_pct:.0f}% of a resting day",
                                  delta=-round(pen, 1), evidence_ids=[dose_id]))  # fmt: skip
    else:
        factors.append(Factor(code=R.air_quality, label="air quality forecast not available for this time",
                              delta=-w.AQ_UNKNOWN_PENALTY))  # fmt: skip

    # -- pollution-source exposure (near-road NO2 / ultrafines beyond the PM2.5 uplift)
    e_mean = sum(h.exposure for h in hs) / len(hs)
    mult = w.EXPOSURE_SENSITIVE_MULT if "avoid_near_road_hours" in ctx.health.extras else 1.0
    pen = w.EXPOSURE_PENALTY_MAX * e_mean * mult
    if pen >= 0.5:
        peak = max(hs, key=lambda h: h.exposure)
        ids = [st.exposure_ids[peak.t], *peak.top_source_ids[:2], c(peak, "wind_dir_deg")]
        factors.append(Factor(code=R.pollution_exposure, label=f"pollution sources upwind (exposure {e_mean:.2f})",
                              delta=-round(pen, 1), evidence_ids=ids))  # fmt: skip

    # -- heat
    hottest = max(hs, key=lambda h: h.wbgt)
    heat_ids = [c(hottest, "wbgt")]
    if hottest.wbgt >= ctx.wbgt_stop:
        stop_id = _limit_id(ctx, f"WBGT stop for this profile ({act.intensity} intensity)", ctx.wbgt_stop,
                            "°C", "ACSM 2007 / Sports Medicine Australia heat guidance")  # fmt: skip
        gates.append(Factor(code=R.heat, label=f"WBGT {hottest.wbgt:.1f} °C at or above the {ctx.wbgt_stop:g} °C stop",
                            delta=0, gate=True, evidence_ids=[*heat_ids, stop_id]))  # fmt: skip
    pen = w.HEAT_PENALTY_AT_STOP * _ramp(hottest.wbgt, w.HEAT_PENALTY_FLOOR_WBGT, ctx.wbgt_stop)
    if pen >= 0.5:
        factors.append(Factor(code=R.heat, label=f"heat stress: WBGT up to {hottest.wbgt:.1f} °C ({hottest.heat_flag.value.replace('_', ' ')})",
                              delta=-round(pen, 1), evidence_ids=heat_ids))  # fmt: skip
    if (ctx.high_intensity and "avoid_high_intensity_in_heat" in ctx.health.extras
            and hottest.wbgt >= w.HEAT_INTENSITY_WBGT):  # fmt: skip
        factors.append(Factor(code=R.profile, label="high-intensity effort in heat (health profile)",
                              delta=-w.HEAT_INTENSITY_PENALTY, evidence_ids=heat_ids))  # fmt: skip

    # -- UV
    sunniest = max(hs, key=lambda h: h.uv)
    if sunniest.uv >= ctx.health.uv_threshold:
        factors.append(Factor(code=R.uv, label=f"UV index {sunniest.uv:.0f}", delta=-w.UV_PENALTY,
                              evidence_ids=[c(sunniest, "uv")]))  # fmt: skip

    # -- ground condition
    first = hs[0]
    if act.key in w.WET_GROUND_ACTIVITIES and first.rain_24h >= w.WET_GROUND_24H_MM:
        ids = [c(first, "rain_24h")]
        if act.key == "cricket" and first.rain_24h >= w.CRICKET_BLOCK_24H_MM:
            gates.append(Factor(code=R.wet_ground, label="pitch waterlogged after heavy rain", delta=0,
                                gate=True, evidence_ids=ids))  # fmt: skip
        else:
            factors.append(Factor(code=R.wet_ground, label="ground likely wet", delta=-w.WET_GROUND_PENALTY,
                                  evidence_ids=ids))  # fmt: skip
    if act.key == "hiking" and first.rain_48h >= w.SLIPPERY_TRAIL_48H_MM:
        factors.append(Factor(code=R.wet_ground, label="slippery trail after rain", delta=-w.SLIPPERY_TRAIL_PENALTY,
                              evidence_ids=[c(first, "rain_48h")]))  # fmt: skip

    # -- travel (OSRM free-flow x time-of-day multiplier)
    if st.travel is not None:
        mult = travel_multiplier(start - timedelta(minutes=st.travel.minutes))  # departure time
        minutes = st.travel.minutes * mult
        note = ("straight-line estimate; " if st.travel.estimated else "OSRM free-flow; ")
        note += f"× {mult:g} time-of-day traffic heuristic" if mult > 1 else "no traffic multiplier"
        tid = store.value(f"travel time to {v.name}, leaving for {start:%a %H:%M}", round(minutes),
                          "min", SourceKey.osrm, ctx.geo_at, note=note)  # fmt: skip
        pen = min(w.TRAVEL_BASE_MAX, w.TRAVEL_BASE_PER_MIN * minutes)
        excess = minutes - w.TRAVEL_PENALTY_SHARE * act.duration_min
        if excess > 0:
            pen += min(w.TRAVEL_EXCESS_MAX, w.TRAVEL_EXCESS_PER_MIN * excess)
        if "prefer_flat_shaded_close" in ctx.health.extras:
            pen *= w.TRAVEL_SENIOR_MULT
        if pen >= 0.5:
            code = R.rush_hour if mult >= w.TRAVEL_PEAK else R.travel_time
            label = f"{minutes:.0f} min travel" + (" in rush hour" if code is R.rush_hour else "")
            factors.append(Factor(code=code, label=label, delta=-round(pen, 1), evidence_ids=[st.ref, tid]))

    # -- access
    if v.private:
        aid = store.value(f"access at {v.name}", v.access or "private", None, SourceKey.overpass, ctx.geo_at)
        factors.append(Factor(code=R.access, label="members / private access: check entry", delta=-w.PRIVATE_ACCESS_PENALTY,
                              evidence_ids=[st.ref, aid]))  # fmt: skip

    # -- bonuses
    lo, hi = w.GREEN_BONUS_SHARE
    bonus = w.GREEN_BONUS_MAX * _ramp(st.green_share, lo, hi)
    if bonus >= 0.5:
        factors.append(Factor(code=R.green_cooling, label=f"green cover {st.green_share:.0%} within 500 m",
                              delta=round(bonus, 1), evidence_ids=[st.micro_ids["green_share"]]))  # fmt: skip
    if act.light == "golden_bonus":
        factors += _photo_bonuses(ctx, st, hs, start, end)

    deltas = sum(f.delta for f in factors)
    return ScoredSlot(
        venue_id=v.id, start=start, end=end, score=round(min(100.0, max(0.0, 100.0 + deltas)), 1),
        blocked=bool(gates), gates=gates, factors=factors,
        pm25_mean=round(pm25_mean, 1) if pm25_mean is not None else None,
        wbgt_max=hottest.wbgt, rain_prob_max=likeliest.rain_prob,
        exposure_mean=round(e_mean, 3), dose_pct=round(dose_pct, 1) if dose_pct is not None else None,
    )  # fmt: skip


def _photo_bonuses(
    ctx: ScoringContext, st: VenueState, hs: list[HourConditions], start: datetime, end: datetime
) -> list[Factor]:
    sun = ctx.sun.get(start.date())
    if sun is None:
        return []
    out = []
    golden = max(sun.golden, key=lambda g: overlap_min((start, end), g))
    blue = max(sun.blue, key=lambda b: overlap_min((start, end), b))
    if overlap_min((start, end), golden) > 0:
        gid = ctx.store.value(f"golden hour on {sun.day:%a %d %b}", f"{golden[0]:%H:%M}–{golden[1]:%H:%M}",
                              "IST", SourceKey.astral, ctx.now)  # fmt: skip
        out.append(Factor(code=R.golden_hour, label="golden-hour light", delta=w.GOLDEN_HOUR_BONUS,
                          evidence_ids=[gid]))  # fmt: skip
        # Clouds at the hour nearest the sun event colour the sky (20-60 % cover).
        event = sun.sunset if golden == sun.golden[1] else sun.sunrise
        near = min(hs, key=lambda h: abs(h.t - event))
        lo, hi = w.CLOUD_COLOUR_RANGE
        if lo <= near.cloud <= hi:
            out.append(Factor(code=R.cloud_colour, label=f"{near.cloud:.0f}% cloud to catch the colour",
                              delta=w.CLOUD_COLOUR_BONUS,
                              evidence_ids=[cite(ctx.store, st.venue, near, "cloud", ctx.weather_at)]))  # fmt: skip
    elif overlap_min((start, end), blue) > 0:
        bid = ctx.store.value(f"blue hour on {sun.day:%a %d %b}", f"{blue[0]:%H:%M}–{blue[1]:%H:%M}",
                              "IST", SourceKey.astral, ctx.now)  # fmt: skip
        out.append(Factor(code=R.golden_hour, label="blue-hour light", delta=w.BLUE_HOUR_BONUS, evidence_ids=[bid]))
    return out


# ---------------------------------------------------------------- hour-level blocks (avoid ranges)


def hour_blocks(ctx: ScoringContext, st: VenueState, hc: HourConditions) -> list[Factor]:
    """Gates that apply to the hour [t, t+1h) on its own (hourly PM2.5 stands in for the
    session mean): what the avoid ranges are built from."""
    v, store = st.venue, ctx.store
    out = []
    if thunder_blocks(ctx, hc):
        out.append(Factor(code=R.thunderstorm, label="thunderstorm", delta=0, gate=True,
                          evidence_ids=[cite(store, v, hc, "weather_code", ctx.weather_at)]))  # fmt: skip
    if rain_blocks(ctx, hc.rain_mm):
        out.append(Factor(code=R.rain, label="heavy rain", delta=0, gate=True,
                          evidence_ids=[cite(store, v, hc, "rain_mm", ctx.weather_at)]))  # fmt: skip
    if wind_blocks(ctx, hc.wind_kmh, hc.gust_kmh):
        out.append(Factor(code=R.wind, label="strong wind", delta=0, gate=True,
                          evidence_ids=[cite(store, v, hc, "gust_kmh", ctx.weather_at)]))  # fmt: skip
    if hc.wbgt >= ctx.wbgt_stop:
        out.append(Factor(code=R.heat, label="heat stress", delta=0, gate=True,
                          evidence_ids=[cite(store, v, hc, "wbgt", ctx.weather_at)]))  # fmt: skip
    if hc.pm25_local is not None and hc.pm25_local > ctx.pm25_limit:
        out.append(Factor(code=R.air_quality, label="air above your limit", delta=0, gate=True,
                          evidence_ids=[st.aq_ids[hc.t]["pm25_local"]]))  # fmt: skip
    limits = light_limits(ctx, v, hc.t.date()) if darkness_gates(ctx, v) else None
    if limits and (hc.t + _HOUR <= limits[0] or hc.t >= limits[1]):
        out.append(Factor(code=R.darkness, label="dark", delta=0, gate=True,
                          evidence_ids=_sun_ids(ctx, hc.t.date())))  # fmt: skip
    sun = ctx.sun.get(hc.t.date())
    if st.opening and sun and not is_open(st.opening, hc.t, hc.t + _HOUR, sun):
        out.append(Factor(code=R.closed, label="closed", delta=0, gate=True, evidence_ids=[st.ref]))
    return out
