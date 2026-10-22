"""Local air quality per venue and hour: CAMS background x ground-station bias (decaying from
the observation time) x exposure uplift, minus a small green bonus; with an uncertainty band
and indicative NAQI category. A modelled estimate, not a measurement.

  PM_local(h) = PM_bg(h) * bias(h) * (1 + 0.35 E(h)) * (1 - green_bonus)
  bias(h)     = 1 + (bias0 - 1) * exp(-(h - t_obs) / 12 h),  bias0 = clamp(obs / CAMS(t_obs))
  band        = 20 % + 10 % per 24 h ahead (+15 % no station, +10 % low-cost sensor used)
"""

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from bahar.config import sources as src
from bahar.config import weights as w
from bahar.engine.evidence import EvidenceStore
from bahar.engine.geo import haversine_km
from bahar.models import (
    HourAirQuality,
    HourExposure,
    NaqiCategory,
    SourceKey,
    StationBias,
    StationReading,
    StationSet,
    Venue,
    VenueAirQuality,
)

MODELLED = "modelled estimate, not a measurement"
_NAQI_ORDER = list(NaqiCategory)


def naqi_category(value: float, table: Sequence[tuple[float, NaqiCategory]]) -> NaqiCategory:
    for upper, category in table:
        if value <= upper:
            return category
    return NaqiCategory.severe


def _station_label(r: StationReading) -> str:
    kind = "regulatory monitor" if r.regulatory else "low-cost sensor"
    return f"{r.provider or 'unknown provider'}; {kind}; observed {r.observed_at.astimezone(src.IST):%d %b %H:%M} IST"


def station_bias(
    venue: Venue,
    stations: StationSet | None,
    cams: VenueAirQuality | None,
    store: EvidenceStore,
) -> StationBias | None:
    """IDW over the venue's nearest <= 3 fresh stations within 15 km (low-cost sensors at half
    weight), compared with CAMS at the venue at the IDW-mean observation hour."""
    if stations is None or cams is None:
        return None
    near = sorted(
        (d, r)
        for r in stations.readings
        if (d := haversine_km(venue.lat, venue.lon, r.lat, r.lon)) <= src.STATION_RADIUS_KM
    )[: src.STATIONS_PER_VENUE]
    if not near:
        return None
    weights = [
        (1.0 if r.regulatory else w.LOW_COST_IDW_WEIGHT) / max(d, w.IDW_MIN_DIST_KM) ** w.IDW_POWER
        for d, r in near
    ]
    total = sum(weights)
    obs = sum(wt * r.pm25_ugm3 for wt, (_, r) in zip(weights, near, strict=True)) / total
    t_obs_s = sum(wt * r.observed_at.timestamp() for wt, (_, r) in zip(weights, near, strict=True)) / total
    t_obs = datetime.fromtimestamp(t_obs_s, UTC).astimezone(src.IST)

    match = min(
        (h for h in cams.hours if h.pm25 is not None),
        key=lambda h: abs((h.t - t_obs).total_seconds()), default=None,
    )  # fmt: skip
    if match is None or abs((match.t - t_obs).total_seconds()) > w.CAMS_MATCH_MAX_H * 3600:
        return None
    if match.pm25 is None or match.pm25 <= 0:
        return None
    lo, hi = w.STATION_BIAS_CLAMP
    bias0 = min(hi, max(lo, obs / match.pm25))

    station_ids = [
        store.value(
            f"PM2.5 measured at {r.name}, {d:.1f} km from {venue.name}", r.pm25_ugm3, "µg/m³",
            SourceKey(r.source), stations.fetched_at, note=_station_label(r),
        )
        for d, r in near
    ]  # fmt: skip
    eid = store.value(
        f"ground-station correction factor for {venue.name}", round(bias0, 2), "×",
        SourceKey.engine, stations.fetched_at,
        note=f"stations {obs:.1f} vs CAMS {match.pm25:.1f} µg/m³ at {match.t:%d %b %H:%M} IST; "
        f"fades with a {w.STATION_BIAS_DECAY_H:g} h time constant",
    )  # fmt: skip
    return StationBias(
        venue_id=venue.id, station_pm25=round(obs, 1), cams_pm25=match.pm25,
        bias0=round(bias0, 3), t_obs=t_obs,
        low_cost=any(not r.regulatory for _, r in near),
        station_evidence_ids=station_ids, evidence_id=eid,
    )  # fmt: skip


def green_bonus(green_share: float) -> float:
    """0 up to GREEN_SHARE_MIN, then linear to GREEN_PM_BONUS_MAX at GREEN_PM_BONUS_FULL_SHARE."""
    if green_share <= w.GREEN_SHARE_MIN:
        return 0.0
    ramp = (green_share - w.GREEN_SHARE_MIN) / (w.GREEN_PM_BONUS_FULL_SHARE - w.GREEN_SHARE_MIN)
    return w.GREEN_PM_BONUS_MAX * min(1.0, ramp)


def _bias_at(bias: StationBias | None, t: datetime) -> float:
    if bias is None:
        return 1.0
    # The decay starts at the observation, not at now; hours before t_obs keep bias0.
    hours_after = max(0.0, (t - bias.t_obs) / timedelta(hours=1))
    return 1.0 + (bias.bias0 - 1.0) * math.exp(-hours_after / w.STATION_BIAS_DECAY_H)


def _band(bias: StationBias | None, t: datetime, now: datetime) -> float:
    ahead_days = max(0.0, (t - now) / timedelta(hours=24))
    band = w.BAND_BASE + w.BAND_PER_DAY * ahead_days
    if bias is None:
        band += w.BAND_NO_STATION
    elif bias.low_cost:
        band += w.BAND_LOW_COST
    return band


def local_air_quality(
    venue: Venue,
    exposure: Sequence[HourExposure],
    cams: VenueAirQuality | None,
    bias: StationBias | None,
    green_share: float,
    now: datetime,
    store: EvidenceStore,
    cams_fetched_at: datetime,
) -> list[HourAirQuality]:
    """Local PM per exposure hour. Hours beyond the CAMS horizon stay unknown (None)."""
    by_t = {h.t: h for h in cams.hours} if cams else {}
    bonus = green_bonus(green_share)
    out = []
    for ex in exposure:
        bg = by_t.get(ex.t)
        if bg is None or bg.pm25 is None:
            out.append(HourAirQuality(venue_id=venue.id, t=ex.t))
            continue
        b = _bias_at(bias, ex.t)
        factor = b * (1.0 + w.EXPOSURE_PM_UPLIFT * ex.exposure) * (1.0 - bonus)
        pm25 = bg.pm25 * factor
        pm10 = bg.pm10 * factor if bg.pm10 is not None else None  # PM2.5 correction reused
        band = _band(bias, ex.t, now)
        low, high = max(0.0, pm25 * (1 - band)), pm25 * (1 + band)
        cats = [naqi_category(pm25, w.NAQI_PM25)]
        if pm10 is not None:
            cats.append(naqi_category(pm10, w.NAQI_PM10))
        naqi = max(cats, key=_NAQI_ORDER.index)  # NAQI = worst sub-index

        when = f"{venue.name}, {ex.t:%a %H:%M}"
        ids = {
            "pm25_bg": store.value(f"PM2.5 background (CAMS) at {when}", round(bg.pm25, 1),
                                   "µg/m³", SourceKey.open_meteo_air_quality, cams_fetched_at),
            "pm25_local": store.value(f"PM2.5 local estimate at {when}", round(pm25, 1),
                                      "µg/m³", SourceKey.engine, cams_fetched_at, note=MODELLED),
            "pm25_low": store.value(f"PM2.5 local estimate, low end, at {when}", round(low, 1),
                                    "µg/m³", SourceKey.engine, cams_fetched_at, note=MODELLED),
            "pm25_high": store.value(f"PM2.5 local estimate, high end, at {when}", round(high, 1),
                                     "µg/m³", SourceKey.engine, cams_fetched_at, note=MODELLED),
            "naqi": store.value(f"NAQI category at {when}", naqi.value.replace("_", " "), None,
                                SourceKey.engine, cams_fetched_at,
                                note="indicative: NAQI is defined on 24-h averages; " + MODELLED),
        }  # fmt: skip
        if pm10 is not None:
            ids["pm10_local"] = store.value(
                f"PM10 local estimate at {when}", round(pm10, 1), "µg/m³", SourceKey.engine,
                cams_fetched_at, note=MODELLED,
            )  # fmt: skip
        out.append(HourAirQuality(
            venue_id=venue.id, t=ex.t, pm25_bg=bg.pm25, pm10_bg=bg.pm10, bias=round(b, 3),
            pm25_local=round(pm25, 1), pm25_low=round(low, 1), pm25_high=round(high, 1),
            pm10_local=round(pm10, 1) if pm10 is not None else None, naqi=naqi, evidence_ids=ids,
        ))  # fmt: skip
    return out


def air_quality_assumptions(
    stations: StationSet | None, biases: Sequence[StationBias | None], now: datetime
) -> list[str]:
    notes = ["Local PM2.5/PM10 are modelled estimates (CAMS + stations + nearby sources), "
             "not measurements; NAQI categories on hourly values are indicative."]  # fmt: skip
    if any(b is not None for b in biases):
        notes.append("PM10 uses the PM2.5 ground-station correction factor.")
        if any(b is not None and b.low_cost for b in biases):
            notes.append("A low-cost sensor contributed to the station correction at half weight; band widened.")
    if any(b is None for b in biases):
        if stations and stations.stale:
            listed = "; ".join(
                f"{s.name} ({s.distance_km:g} km, last report "
                f"{(now - s.last_seen) / timedelta(hours=1):.0f} h ago)"
                for s in stations.stale
            )
            notes.append(f"No regulatory station reported in the last {src.STATION_MAX_AGE_H:g} h "
                         f"({listed}); no station correction applied, CAMS used as is.")  # fmt: skip
        else:
            notes.append(f"No fresh ground station within {src.STATION_RADIUS_KM:g} km for some "
                         "venues: CAMS used without station correction, wider band.")  # fmt: skip
    return notes
