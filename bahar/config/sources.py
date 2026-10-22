"""Endpoints, rate limits, retry policy and cache TTLs for the data layer."""

import re
from datetime import timedelta, timezone
from typing import Final

IST: Final = timezone(timedelta(hours=5, minutes=30), "IST")

# ---- HTTP policy
RETRIES: Final = 4  # attempts per request (mirrors are rotated between attempts)
BACKOFF_BASE_S: Final = 0.6  # sleep = base * 2^attempt + jitter
BACKOFF_JITTER_S: Final = 0.4
RETRY_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
DEFAULT_TIMEOUT_S: Final = 8.0

# Minimum seconds between requests per host. Nominatim policy: <= 1 req/s; OpenTopoData public
# instance: 1 req/s. Small margin over 1.0 so jitter in scheduling never breaks the limit.
HOST_MIN_INTERVAL_S: Final[dict[str, float]] = {
    "nominatim.openstreetmap.org": 1.1,
    "api.opentopodata.org": 1.1,
}

# Bump when a fetcher's parsing / selection changes so stale cached results are not reused.
CACHE_VERSION: Final = 4

# ---- cache TTLs (CLAUDE.md: live <= 15 min, static geography <= 7 days)
LIVE_TTL_S: Final = 15 * 60
STATIC_TTL_S: Final = 7 * 24 * 3600

# ---- Nominatim
NOMINATIM_URL: Final = "https://nominatim.openstreetmap.org/search"
# Nominatim place_rank: 16 = city / town boundary, >= 17 = suburb, neighbourhood, POI.
CITY_MAX_PLACE_RANK: Final = 16
CITY_MIN_RADIUS_KM: Final = 12.0
MAX_RADIUS_KM: Final = 50.0

# ---- Overpass: mirrors are tried round-robin, one per attempt
OVERPASS_MIRRORS: Final = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_SERVER_TIMEOUT_S: Final = 25  # [timeout:N] ceiling from CLAUDE.md
VENUE_ATTEMPT_TIMEOUT_S: Final = 12.0
VENUE_DEADLINE_S: Final = 25.0
# The context query is the biggest threat to pillar 1 (no context -> E = 0 -> venues stop
# differing). Measured Oct 2026: overpass.openstreetmap.fr answers a 12-venue Mumbai query in
# ~8 s while the other mirrors time out at 40 s. So: 12 s on the last-good mirror, then one
# retry on a different mirror inside a 20 s total, then degrade.
CONTEXT_ATTEMPT_TIMEOUT_S: Final = 12.0
CONTEXT_DEADLINE_S: Final = 20.0
CONTEXT_RADIUS_M: Final = 2000  # roads / industry / power (exposure sources)
MICROCLIMATE_RADIUS_M: Final = 500  # green / water share window

# ---- Open-Meteo
FORECAST_URL: Final = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL: Final = "https://air-quality-api.open-meteo.com/v1/air-quality"
FORECAST_DAYS: Final = 7
PAST_DAYS: Final = 2  # ground-condition rule needs 48 h of past rain
AQ_FORECAST_DAYS: Final = 5  # CAMS Global horizon
# The station bias compares a reading with CAMS at the observation hour, which can be up to
# STATION_MAX_AGE_H in the past (yesterday, shortly after midnight).
AQ_PAST_DAYS: Final = 1
TIMEZONE: Final = "Asia/Kolkata"
FORECAST_HOURLY: Final = (
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature",
    "wet_bulb_temperature_2m", "precipitation_probability", "precipitation", "weather_code",
    "cloud_cover", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "uv_index",
    "shortwave_radiation", "direct_radiation", "cape", "is_day",
)  # fmt: skip
AIR_QUALITY_HOURLY: Final = ("pm2_5", "pm10", "nitrogen_dioxide", "ozone")

# ---- ground stations
WAQI_BOUNDS_URL: Final = "https://api.waqi.info/map/bounds/"
WAQI_FEED_URL: Final = "https://api.waqi.info/feed/@{uid}/"
OPENAQ_LOCATIONS_URL: Final = "https://api.openaq.org/v3/locations"
OPENAQ_LATEST_URL: Final = "https://api.openaq.org/v3/locations/{id}/latest"
OPENAQ_PM25_PARAMETER_ID: Final = 2
STATION_RADIUS_KM: Final = 15.0
STATIONS_PER_VENUE: Final = 3
MAX_STATION_FEEDS: Final = 8  # cap on per-station requests per query
# 6 h, not 3: OpenAQ / WAQI feeds for Indian stations often lag 2-5 h. The bias still decays
# from the observation time (weights.STATION_BIAS_DECAY_H), so an older reading counts less.
STATION_MAX_AGE_H: Final = 6.0
STATION_TIMEOUT_S: Final = 8.0
MAX_STALE_REPORTED: Final = 3  # nearest stale regulatory stations named in the assumptions
# Regulatory / reference-grade monitors, matched against provider, owner and station name:
# CPCB and state boards (MPCB, DPCC, KSPCB, ... "Pollution Control"), IITM / SAFAR, municipal
# CAAQMS (BMC, NMMC), and US Embassy / Consulate monitors (AirNow, BAM reference monitors).
# OpenAQ's isMonitor flag also counts. Anything else is treated as a low-cost sensor.
REGULATORY_PATTERN: Final = re.compile(
    r"\b(CPCB|[A-Z]{1,3}PCB|DPCC|IITM|SAFAR|AirNow|caaqm|BMC|NMMC)\b|Pollution Control"
    r"|Tropical Meteorology|U\.?S\.? (Embassy|Consulate)|Municipal Corporation",
    re.IGNORECASE,
)

# US EPA PM2.5 AQI breakpoints (pre-2024 table, which WAQI documents at aqicn.org/faq):
# (conc_lo, conc_hi, aqi_lo, aqi_hi). WAQI publishes sub-indices, not ug/m3, so we invert.
EPA_PM25_BREAKPOINTS: Final = (
    (0.0, 12.0, 0, 50),
    (12.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
)

# ---- routing (OSRM table service)
OSRM_URLS: Final[dict[str, str]] = {
    "car": "https://router.project-osrm.org/table/v1/driving/",
    "bike": "https://routing.openstreetmap.de/routed-bike/table/v1/driving/",
    "foot": "https://routing.openstreetmap.de/routed-foot/table/v1/driving/",
}
ROUTING_TIMEOUT_S: Final = 8.0
# Fallback when OSRM is down: haversine x detour factor / mean speed. Typical urban values.
HAVERSINE_DETOUR: Final = 1.4
FALLBACK_SPEED_KMH: Final[dict[str, float]] = {"car": 25.0, "bike": 14.0, "foot": 4.5}

# ---- elevation
OPENTOPODATA_URL: Final = "https://api.opentopodata.org/v1/srtm30m"
OPENTOPODATA_MAX_POINTS: Final = 100
