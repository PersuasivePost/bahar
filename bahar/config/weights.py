"""Engine thresholds and weights (CLAUDE.md domain spec). Documented heuristic defaults."""

from typing import Final

from bahar.models import Activity, HeatFlag, NaqiCategory, Part

# ---------------------------------------------------------------- time windows (IST)

# Sets of session START hours, inclusive; a session may run past the window end.
PART_START_HOURS: Final[dict[Part, frozenset[int]]] = {
    Part.early_morning: frozenset(range(5, 8)),
    Part.morning: frozenset(range(6, 11)),
    Part.midday: frozenset(range(11, 15)),
    Part.afternoon: frozenset(range(12, 17)),
    Part.evening: frozenset(range(16, 20)),
    Part.night: frozenset(range(19, 23)),
    Part.any: frozenset(range(24)),
}
DEFAULT_WINDOW_H: Final = 24  # no time given -> next 24 h
DEFAULT_N_DAYS: Final = 3  # "next few days" without a number
EXPLICIT_HOUR_SLACK_H: Final = 1  # "7 baje" -> starts 06:00-08:00, so an alt time exists
# "around sunrise / sunset": starts from 90 min before to 30 min after the event, which
# covers the golden hour (sun elevation -4 deg to 6 deg is roughly +-40 min around it).
AROUND_SUN_BEFORE_MIN: Final = 90
AROUND_SUN_AFTER_MIN: Final = 30
# Used only when the caller has no forecast sunrise/sunset: Mumbai annual means (IST).
FALLBACK_SUNRISE: Final = (6, 30)
FALLBACK_SUNSET: Final = (18, 30)
WEEKEND_DAYS: Final = frozenset({5, 6})  # date.weekday(): Sat, Sun

# ---------------------------------------------------------------- exposure index

SOURCE_WEIGHTS: Final[dict[str, float]] = {
    "motorway": 1.0, "trunk": 0.8, "primary": 0.5, "secondary": 0.25,
    "industrial": 0.9,  # x min(1, sqrt(area_km2)): a small industrial plot emits less
    "landfill": 1.0, "construction": 0.35, "quarry": 0.6, "power_plant": 1.0,
}  # fmt: skip  # construction 0.35: OSM construction tags are often stale
AREA_SCALED_SOURCES: Final = frozenset({"industrial"})
ROAD_SOURCES: Final = frozenset({"motorway", "trunk", "primary", "secondary"})
SOURCE_GROUPS: Final = ("road", "industry", "power")  # ContextFeature groups that emit
EXPOSURE_TOP_GROUPS: Final = 8
EXPOSURE_TOP_CONTRIBUTORS: Final = 3
# Near-road pollutant gradients largely fade within a few hundred metres (Karner, Eisinger &
# Niemeier 2010, Environ. Sci. Technol. 44:5334): e-folding distance 400 m.
DISTANCE_DECAY_M: Final = 400.0
UPWIND_SIGMA_DEG: Final = 30.0  # Gaussian width of the upwind alignment a = exp(-d^2/2s^2)
# Calm-wind blend instead of a step at 1 m/s: blend = clamp((u - 0.5) / 1.5, 0, 1) mixes the
# directional terms with the stagnant ones (plumes spread in all directions, little dilution).
CALM_BLEND_U0_MS: Final = 0.5  # fully stagnant at or below
CALM_BLEND_SPAN_MS: Final = 1.5  # fully directional from 2.0 m/s
STAGNANT_ALIGNMENT: Final = 0.5
STAGNATION_FACTOR: Final = 1.3
# Venue centroid inside the source polygon (distance ~0, bearing undefined): the source
# surrounds the venue, so any wind carries it in -> full alignment.
INSIDE_SOURCE_M: Final = 1.0
INSIDE_ALIGNMENT: Final = 1.0
# Contributions below this are not cited as evidence (a road 1.9 km away crosswind adds ~0).
EXPOSURE_MIN_CONTRIBUTION: Final = 0.01
# Dilution s = clamp(DILUTION_K / (u + DILUTION_U0), lo, hi): ~1/u with a calm-wind offset.
DILUTION_K: Final = 3.0
DILUTION_U0_MS: Final = 2.0
DILUTION_CLAMP: Final = (0.4, 1.0)
ROAD_PEAK_FACTOR: Final = 1.3  # hours whose travel multiplier is TRAVEL_PEAK
ROAD_NIGHT_FACTOR: Final = 0.6
ROAD_NIGHT_HOURS: Final = frozenset({23, 0, 1, 2, 3, 4, 5})  # 23:00-06:00
CONSTRUCTION_ACTIVE_HOURS: Final = frozenset(range(8, 20))  # 08:00-20:00 work hours
CONSTRUCTION_IDLE_FACTOR: Final = 0.3  # no dust-raising work at night
# Corridor dedupe: same-class groups whose nearest points are this close are one corridor
# (dual carriageways, unnamed slip roads, twin names like JVLR / Adi Sankaracharya Marg).
SOURCE_MERGE_M: Final = 60.0

# ---------------------------------------------------------------- travel / traffic heuristic
# No free live traffic exists for India; disclosed heuristic on OSRM free-flow durations.

TRAVEL_PEAK: Final = 1.7
TRAVEL_SHOULDER: Final = 1.3
TRAVEL_WEEKEND_DAY: Final = 1.2
WEEKDAY_PEAK_HOURS: Final = frozenset({8, 9, 10, 17, 18, 19, 20})  # 08-11, 17-21
WEEKDAY_SHOULDER_HOURS: Final = frozenset({7, 11, 16, 21})
WEEKEND_DAYTIME_HOURS: Final = frozenset(range(7, 22))
TRAVEL_PENALTY_SHARE: Final = 0.5  # penalty when travel > 0.5 x activity duration

# ---------------------------------------------------------------- air quality fusion

STATION_BIAS_CLAMP: Final = (0.5, 2.5)
# bias(h) = 1 + (bias0 - 1) * exp(-(h - t_obs) / 12 h): the station correction fades as the
# forecast moves away from the station's OBSERVATION time (not from "now"), since a 5-h-old
# reading already describes a past hour. t_obs = IDW-weighted mean observation time.
STATION_BIAS_DECAY_H: Final = 12.0
EXPOSURE_PM_UPLIFT: Final = 0.35  # PM_local = PM_bg * bias * (1 + 0.35 E)
GREEN_SHARE_MIN: Final = 0.40  # green share within 500 m above which the bonus applies
GREEN_PM_BONUS_MAX: Final = 0.05  # at most 5 % lower PM (vegetation deposition, modest)
# The bonus ramps linearly from 0 at GREEN_SHARE_MIN to the max at this share.
GREEN_PM_BONUS_FULL_SHARE: Final = 0.80
# Station IDW over the nearest sources.STATIONS_PER_VENUE stations within
# sources.STATION_RADIUS_KM: weight = quality / max(d, floor)^p. p = 2 is the common IDW
# default; the 0.5 km floor keeps a station next to the venue from taking all the weight.
IDW_POWER: Final = 2.0
IDW_MIN_DIST_KM: Final = 0.5
# Low-cost sensors (uncorrected optical PM) read high in humid air; half IDW weight and a
# wider band. Regulatory / reference monitors (CPCB, SPCB, IITM, US consulate) weigh 1.
LOW_COST_IDW_WEIGHT: Final = 0.5
BAND_LOW_COST: Final = 0.10
# Observation must be matched to a CAMS hour within this many hours.
CAMS_MATCH_MAX_H: Final = 1.0
BAND_BASE: Final = 0.20
BAND_PER_DAY: Final = 0.10  # +10 % per 24 h ahead
BAND_NO_STATION: Final = 0.15

# CPCB National AQI breakpoints (µg/m³, 24-h averages; hourly values are "indicative").
# (upper bound inclusive, category); above the last bound -> severe.
NAQI_PM25: Final[tuple[tuple[float, NaqiCategory], ...]] = (
    (30, NaqiCategory.good), (60, NaqiCategory.satisfactory), (90, NaqiCategory.moderate),
    (120, NaqiCategory.poor), (250, NaqiCategory.very_poor),
)  # fmt: skip
NAQI_PM10: Final[tuple[tuple[float, NaqiCategory], ...]] = (
    (50, NaqiCategory.good), (100, NaqiCategory.satisfactory), (250, NaqiCategory.moderate),
    (350, NaqiCategory.poor), (430, NaqiCategory.very_poor),
)  # fmt: skip

# ---------------------------------------------------------------- microclimate
# Share windows use sources.MICROCLIMATE_RADIUS_M (500 m). Near water = mapped water within
# 300 m (sea breeze / evaporative cooling and insects near dusk).
NEAR_WATER_M: Final = 300.0

# ---------------------------------------------------------------- inhaled dose
# Reference day = 60 µg/m³ (top of NAQI "Satisfactory") x 8 L/min (resting, US EPA Exposure
# Factors Handbook) x 1440 min / 1000 ≈ 691 µg.
DOSE_REF_PM25: Final = 60.0
DOSE_REF_VENTILATION_LPM: Final = 8.0
DOSE_REF_MIN: Final = 1440

# ---------------------------------------------------------------- heat
# WBGT flag bands (°C, lower bound inclusive), after the ACSM / US military flag system.
WBGT_FLAGS: Final[tuple[tuple[float, HeatFlag], ...]] = (
    (32, HeatFlag.extreme), (30, HeatFlag.very_high), (28, HeatFlag.high),
    (23, HeatFlag.moderate), (float("-inf"), HeatFlag.low),
)  # fmt: skip

# Globe-temperature energy balance (Liljegren et al. 2008, J. Occup. Environ. Hyg. 5:645,
# eq. for the black globe), standard 150 mm globe; forced convection per ISO 7726 (1998)
# h = 6.3 u^0.6 / D^0.4 W/m2K. Open-Meteo wind is at 10 m; scaled to 2 m with a log profile
# over z0 = 0.1 m (ln(2/0.1) / ln(10/0.1) = 0.65). Liljegren's 0.5 m/s floor on wind.
GLOBE_DIAMETER_M: Final = 0.15
GLOBE_EMISSIVITY: Final = 0.95
GLOBE_ALBEDO: Final = 0.05
SURFACE_ALBEDO: Final = 0.45
WIND_10M_TO_2M: Final = 0.65
MIN_WIND_MS: Final = 0.5
# Below ~5 deg sun elevation the 1/cos(zenith) direct-beam term diverges; Liljegren caps the
# zenith angle at 89.5 deg, we cap cos(zenith) at sin(5 deg) since hourly radiation is tiny then.
MIN_COS_ZENITH: Final = 0.087
ABSOLUTE_ZERO_C: Final = -273.15

# ---------------------------------------------------------------- sun / light
# astral conventions: golden hour = sun elevation -4..6 deg, blue hour = -6..-4 deg.
GOLDEN_ELEV: Final = (-4.0, 6.0)
BLUE_ELEV: Final = (-6.0, -4.0)

# ---------------------------------------------------------------- weather gates

THUNDER_CODES: Final = frozenset({95, 96, 99})  # WMO weather codes (Open-Meteo)
THUNDER_BLOCKED: Final = frozenset({
    Activity.golf, Activity.cricket, Activity.football, Activity.tennis, Activity.hiking,
    Activity.event, Activity.picnic,
})  # fmt: skip
# CAPE >= 1500 J/kg is "moderately unstable" in standard forecaster guidance (e.g. NOAA SPC).
CAPE_WARN_JKG: Final = 1500.0
CAPE_WARN_RAIN_PROB: Final = 40.0
RAIN_BLOCK_LIGHT_MM: Final = 1.0  # blocks RAIN_LIGHT_BLOCKED
RAIN_LIGHT_BLOCKED: Final = frozenset({Activity.tennis, Activity.cricket, Activity.event})
RAIN_BLOCK_MM: Final = 2.5  # blocks everything else (AMS glossary: > 2.5 mm/h is "moderate rain")
WIND_PENALTY_SHARE: Final = 0.8  # penalty above 80 % of the activity limit
DUSK_GRACE_MIN: Final = 30  # dusk_ok activities may run until sunset + 30 min
# ... and may start from sunrise - 30 min (civil dawn: enough light to see the path).
DAWN_GRACE_MIN: Final = 30
# Running / walking / cycling (light rule dusk_ok) beyond the dusk grace: urban parks and
# tracks are usually used after dark, so darkness is a soft penalty, not a gate — unless the
# venue is mapped lit=no or is not urban (no street lighting, uneven ground, safety).
DARK_PENALTY_LIT: Final = 6.0  # lit=yes
DARK_PENALTY_UNLIT_UNKNOWN: Final = 12.0  # no lit tag
NON_URBAN_KINDS: Final = frozenset(
    {"nature_reserve", "protected_area", "national_park", "fort", "peak", "beach"}
)  # a beach counts as urban only when tagged lit=yes
EVENING_OK_END_HOUR: Final = 23  # evening_ok: the night part's last start is 22:00

# ---------------------------------------------------------------- ground condition

WET_GROUND_24H_MM: Final = 10.0
WET_GROUND_ACTIVITIES: Final = frozenset(
    {Activity.cricket, Activity.football, Activity.golf, Activity.picnic}
)
CRICKET_BLOCK_24H_MM: Final = 25.0
SLIPPERY_TRAIL_48H_MM: Final = 15.0

# ---------------------------------------------------------------- advice triggers

ADVICE_WATER_WBGT: Final = 25.0
ADVICE_WATER_DURATION_MIN: Final = 60
ADVICE_ELECTROLYTES_WBGT: Final = 28.0
ADVICE_SUNSCREEN_UV: Final = 6.0
ADVICE_RAIN_GEAR_PROB: Final = (30.0, 60.0)
ADVICE_N95_PM25: Final = 60.0

# Advice: high exposure means a strong upwind source (E = tanh(E_raw) >= 0.5, E_raw >= 0.55,
# e.g. a trunk road ~150 m upwind at moderate wind).
ADVICE_N95_EXPOSURE: Final = 0.5
ADVICE_DUSK_WINDOW_MIN: Final = 60  # insects: session within an hour of sunset
ADVICE_GREEN_SHARE: Final = 0.40
WIND_SHIFT_DEG: Final = 45.0  # watch_for: wind turns by at least this between hours
WIND_SHIFT_MIN_KMH: Final = 5.0  # ... at a speed where direction means something
HEAT_WATCH_LOOKAHEAD_H: Final = 3  # heat rising: look this far past the session end

# ---------------------------------------------------------------- scoring (soft score 0-100)
# Points are heuristic weights chosen so that a hard-gate-level problem costs about as much as
# the largest single penalty, and the ordering of the factors matches health priority:
# air / heat > storms / rain > travel > comfort bonuses.

# Air quality: 0 at 30 µg/m³ (top of NAQI "Good"), linear to AQ_PENALTY_AT_LIMIT at the
# profile's hard limit, same slope above it (so the least-bad of all-gated slots is the
# cleanest one).
AQ_PENALTY_FLOOR: Final = 30.0
AQ_PENALTY_AT_LIMIT: Final = 40.0
# Session AQ unknown (beyond the CAMS horizon): a mid-range penalty, so unknown air does not
# out-rank known moderate air.
AQ_UNKNOWN_PENALTY: Final = 15.0
# Near-road pollutants that the PM2.5 uplift misses (NO2, ultrafine particles; Karner 2010):
# penalty = max * session-mean E. Profiles with "avoid_near_road_hours" weigh it 1.5x.
EXPOSURE_PENALTY_MAX: Final = 10.0
EXPOSURE_SENSITIVE_MULT: Final = 1.5
# Inhaled dose share of the reference day (soft only, CLAUDE.md "Inhaled dose").
DOSE_PENALTY_PER_PCT: Final = 0.1
DOSE_PENALTY_MAX: Final = 10.0
# Heat: 0 at WBGT 23 °C (flag "moderate" starts), linear to HEAT_PENALTY_AT_STOP at the
# profile's WBGT stop.
HEAT_PENALTY_FLOOR_WBGT: Final = 23.0
HEAT_PENALTY_AT_STOP: Final = 25.0
UV_PENALTY: Final = 6.0
THUNDER_PENALTY: Final = 40.0  # activities thunder does not block outright
CAPE_PENALTY: Final = 10.0
# Above the activity max: base + per % over it (small base: no cliff at the limit itself).
RAIN_PROB_PENALTY_BASE: Final = 2.0
RAIN_PROB_PENALTY_PER_PCT: Final = 0.5
RAIN_PROB_PENALTY_MAX: Final = 25.0
WIND_PENALTY: Final = 10.0
WET_GROUND_PENALTY: Final = 10.0
SLIPPERY_TRAIL_PENALTY: Final = 8.0
# Travel: a gentle preference for closer venues, then a steeper one beyond
# TRAVEL_PENALTY_SHARE x activity duration.
TRAVEL_BASE_PER_MIN: Final = 0.1
TRAVEL_BASE_MAX: Final = 6.0
TRAVEL_EXCESS_PER_MIN: Final = 0.4
TRAVEL_EXCESS_MAX: Final = 20.0
TRAVEL_SENIOR_MULT: Final = 1.5  # "prefer close venues"
PRIVATE_ACCESS_PENALTY: Final = 10.0  # members-only golf courses etc.: entry not guaranteed
# Heart / pregnancy "avoid high intensity in heat": high-intensity sessions from WBGT 25 °C
# (heat-stress risk rises sharply from the moderate flag up).
HEAT_INTENSITY_WBGT: Final = 25.0
HEAT_INTENSITY_PENALTY: Final = 15.0
# Bonuses
GREEN_BONUS_MAX: Final = 6.0  # shade and cooler air
GREEN_BONUS_SHARE: Final = (0.2, 0.6)  # ramp from / full at this green share within 500 m
GOLDEN_HOUR_BONUS: Final = 15.0
BLUE_HOUR_BONUS: Final = 8.0
CLOUD_COLOUR_BONUS: Final = 5.0
CLOUD_COLOUR_RANGE: Final = (20.0, 60.0)  # % cover that lights up a sunset / sunrise sky
# Hard-gate priority when merging blocked hours into avoid ranges (most serious first).
GATE_PRIORITY: Final = (
    "thunderstorm", "rain", "wind", "heat", "air_quality", "darkness", "closed", "outside_radius",
)  # fmt: skip
MAX_AVOID_RANGES: Final = 3  # longest first

# ---------------------------------------------------------------- recommendation
ALT_TIME_MIN_GAP_H: Final = 2  # prefer an alternative time that does not overlap the best
IMPROVE_SEARCH_H: Final = 48  # no_safe_window: look this far past the window for a safe start
# Confidence starts at 1 and drops per degraded source (stage name -> penalty).
CONFIDENCE_PENALTY: Final[dict[str, float]] = {
    "context": 0.2, "airquality": 0.2, "stations": 0.1, "routing": 0.05, "forecast": 0.5,
}  # fmt: skip
CONFIDENCE_FLOOR: Final = 0.2
