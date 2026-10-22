"""All pydantic models at module boundaries (docs/plan.md §2)."""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Activity(StrEnum):
    running = "running"
    walking = "walking"
    cycling = "cycling"
    golf = "golf"
    cricket = "cricket"
    football = "football"
    tennis = "tennis"
    hiking = "hiking"
    picnic = "picnic"
    event = "event"
    photography = "photography"


class Lang(StrEnum):
    en = "en"
    hinglish = "hinglish"
    hi = "hi"
    mr = "mr"


class Part(StrEnum):
    early_morning = "early_morning"
    morning = "morning"
    midday = "midday"
    afternoon = "afternoon"
    evening = "evening"
    night = "night"
    any = "any"


class Condition(StrEnum):
    asthma = "asthma"
    copd = "copd"
    heart = "heart"
    pregnancy = "pregnancy"
    other = "other"


class NaqiCategory(StrEnum):
    good = "good"
    satisfactory = "satisfactory"
    moderate = "moderate"
    poor = "poor"
    very_poor = "very_poor"
    severe = "severe"


class HeatFlag(StrEnum):
    low = "low"
    moderate = "moderate"
    high = "high"
    very_high = "very_high"
    extreme = "extreme"


class ReasonCode(StrEnum):
    thunderstorm = "thunderstorm"
    rain = "rain"
    wind = "wind"
    darkness = "darkness"
    air_quality = "air_quality"
    heat = "heat"
    uv = "uv"
    closed = "closed"
    outside_radius = "outside_radius"
    pollution_exposure = "pollution_exposure"
    inhaled_dose = "inhaled_dose"
    wet_ground = "wet_ground"
    travel_time = "travel_time"
    green_cooling = "green_cooling"
    golden_hour = "golden_hour"
    cloud_colour = "cloud_colour"
    rush_hour = "rush_hour"
    profile = "profile"
    access = "access"


class SourceKey(StrEnum):
    nominatim = "nominatim"
    overpass = "overpass"
    open_meteo_forecast = "open_meteo_forecast"
    open_meteo_air_quality = "open_meteo_air_quality"
    waqi = "waqi"
    openaq = "openaq"
    osrm = "osrm"
    opentopodata = "opentopodata"
    astral = "astral"
    engine = "engine"


class Status(StrEnum):
    ok = "ok"
    no_safe_window = "no_safe_window"
    needs_clarification = "needs_clarification"
    out_of_scope = "out_of_scope"
    no_venues = "no_venues"


# ---------------------------------------------------------------- parser output


class TimeSpec(BaseModel):
    # day: today | tomorrow | day_after | weekday:<mon..sun> | this_weekend | next_weekend |
    #      next_n_days | date:YYYY-MM-DD
    day: str | None = None
    n_days: int | None = Field(default=None, ge=1, le=7)
    part: Part | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    around: Literal["sunrise", "sunset"] | None = None


class Profile(BaseModel):
    age_group: Literal["child", "adult", "senior"] | None = None
    conditions: list[Condition] = []
    group_size: int | None = Field(default=None, ge=1)


class Commute(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class ParsedQuery(BaseModel):
    in_scope: bool
    activity: Activity | None = None
    activity_raw: str | None = None
    location_text: str | None = None
    radius_km: float | None = None
    time: TimeSpec = TimeSpec()
    profile: Profile = Profile()
    preferences: list[str] = []
    commute: Commute | None = None
    lang: Lang


class TimeWindow(BaseModel):
    """Resolved TimeSpec: candidate session start hours (IST, on the hour, never past)."""

    starts: list[datetime]
    label: str
    assumptions: list[str] = []


# ---------------------------------------------------------------- fetched geography


class Venue(BaseModel):
    id: str  # "osm:way/123"
    name: str
    kind: str
    lat: float
    lon: float
    area_km2: float | None = None
    bbox: tuple[float, float, float, float] | None = None  # south, west, north, east
    distance_km: float
    tags: dict[str, str] = {}
    opening_hours: str | None = None
    access: str | None = None
    lit: bool | None = None
    private: bool = False


class PollutionSource(BaseModel):
    cls: str
    name: str | None = None  # OSM name, else ref; None for unnamed features
    aliases: list[str] = []  # names of same-class groups merged into this one (one corridor)
    weight: float
    distance_m: float  # venue centroid -> nearest point of the source group
    bearing_deg: float  # venue -> that nearest point
    lat: float  # the nearest point itself
    lon: float
    evidence_id: str


class VenueContext(BaseModel):
    venue_id: str
    sources: list[PollutionSource] = []  # grouped by (class, name/ref), top 8
    green_share_500m: float = 0.0
    water_share_500m: float = 0.0
    near_water: bool = False
    water_distance_m: float | None = None  # None = no water mapped within 500 m
    travel_min: float | None = None
    travel_estimated: bool = False
    evidence_ids: dict[str, str] = {}  # "green_share" | "water_share" | "water_distance" -> id


class Place(BaseModel):
    """A geocoded location (Nominatim)."""

    query: str
    name: str  # Nominatim display_name
    lat: float
    lon: float
    bbox: tuple[float, float, float, float]  # south, west, north, east
    place_rank: int
    is_city: bool


# One geometry = a polyline or ring as (lat, lon) pairs.
LatLon = tuple[float, float]


class ContextFeature(BaseModel):
    """A raw OSM feature near the candidates; the engine groups and weighs them later."""

    osm_id: str  # "way/123"
    group: Literal["road", "industry", "power", "green", "water"]
    cls: str  # motorway | trunk | primary | secondary | industrial | landfill | construction |
    #           quarry | power_plant | green | water | coastline | river
    name: str | None = None
    ref: str | None = None
    closed: bool  # True = polygon ring(s), False = polyline
    rings: list[list[LatLon]]


class WeatherHour(BaseModel):
    t: datetime  # IST
    temp_c: float | None = None
    rh: float | None = None
    dew_c: float | None = None
    apparent_c: float | None = None
    wet_bulb_c: float | None = None
    rain_prob: float | None = None  # null for past hours
    rain_mm: float | None = None
    weather_code: int | None = None
    cloud: float | None = None
    wind_kmh: float | None = None
    wind_dir_deg: float | None = None
    gust_kmh: float | None = None
    uv: float | None = None
    swr: float | None = None
    direct_rad: float | None = None
    cape: float | None = None
    is_day: bool | None = None


class VenueWeather(BaseModel):
    venue_id: str
    lat: float  # grid point actually used by Open-Meteo
    lon: float
    elevation_m: float | None = None
    hours: list[WeatherHour]
    sunrise: dict[date, datetime] = {}
    sunset: dict[date, datetime] = {}


class AirQualityHour(BaseModel):
    t: datetime  # IST
    pm25: float | None = None  # null beyond the CAMS horizon
    pm10: float | None = None
    no2: float | None = None
    o3: float | None = None


class VenueAirQuality(BaseModel):
    venue_id: str
    hours: list[AirQualityHour]


class StationReading(BaseModel):
    uid: str
    name: str
    lat: float
    lon: float
    pm25_ugm3: float
    aqi_pm25: int | None = None  # WAQI sub-index the value was inverted from
    observed_at: datetime
    source: Literal["waqi", "openaq"]
    provider: str | None = None  # originator / owner, e.g. "CPCB / Maharashtra Pollution Control Board"
    regulatory: bool  # reference monitor (CPCB, SPCB, IITM, US consulate) vs low-cost sensor


class StaleStation(BaseModel):
    """A station inside the search radius whose last report is older than the freshness limit."""

    name: str
    provider: str | None = None
    regulatory: bool
    last_seen: datetime
    distance_km: float  # to the nearest candidate venue


class StationSet(BaseModel):
    fetched_at: datetime
    readings: list[StationReading]  # fresh only
    stale: list[StaleStation] = []  # nearest stale ones, kept when nothing fresh exists


class TravelTime(BaseModel):
    venue_id: str
    minutes: float  # free-flow; the traffic multiplier is applied by the engine
    estimated: bool  # True = haversine fallback, not routed


class Forecast(BaseModel):
    fetched_at: datetime
    venues: dict[str, VenueWeather]


class AirQualityForecast(BaseModel):
    fetched_at: datetime
    venues: dict[str, VenueAirQuality]


class ContextData(BaseModel):
    fetched_at: datetime
    features: list[ContextFeature]


# ---------------------------------------------------------------- engine output


class SourceContribution(BaseModel):
    source: int  # index into VenueContext.sources
    term: float  # w * a * k * s * traffic: this source's share of E_raw
    evidence_id: str


class HourExposure(BaseModel):
    venue_id: str
    t: datetime  # IST
    exposure: float  # E(h) = tanh(E_raw) in [0, 1)
    top: list[SourceContribution]  # up to 3, largest first
    evidence_id: str


class StationBias(BaseModel):
    """Ground-station correction for one venue: bias0 at t_obs, decaying afterwards."""

    venue_id: str
    station_pm25: float  # IDW mean of the stations used
    cams_pm25: float  # CAMS at the venue at the observation hour
    bias0: float  # clamped ratio
    t_obs: datetime  # IDW-weighted mean observation time
    low_cost: bool  # a low-cost sensor contributed
    station_evidence_ids: list[str]
    evidence_id: str


class HourAirQuality(BaseModel):
    venue_id: str
    t: datetime  # IST
    pm25_bg: float | None = None  # None beyond the CAMS horizon -> AQ unknown
    pm10_bg: float | None = None
    bias: float = 1.0
    pm25_local: float | None = None
    pm25_low: float | None = None
    pm25_high: float | None = None
    pm10_local: float | None = None
    naqi: NaqiCategory | None = None  # worse of the PM2.5 and PM10 categories, indicative
    evidence_ids: dict[str, str] = {}  # field name -> evidence id


class HourConditions(BaseModel):
    venue_id: str
    t: datetime  # IST
    temp_c: float
    rh: float
    dew_c: float
    wind_kmh: float
    wind_dir_deg: float
    gust_kmh: float
    rain_prob: float
    rain_mm: float
    weather_code: int
    cloud: float
    uv: float
    swr: float
    cape: float
    is_day: bool
    pm25_bg: float | None = None  # None beyond the AQ horizon
    pm10_bg: float | None = None
    bias: float = 1.0
    exposure: float = 0.0
    top_source_ids: list[str] = []
    pm25_local: float | None = None
    pm25_low: float | None = None
    pm25_high: float | None = None
    pm10_local: float | None = None
    naqi: NaqiCategory | None = None
    wbgt: float
    heat_flag: HeatFlag
    sun_elev: float
    sun_az: float
    light: Literal["day", "golden", "blue", "dark"]
    rain_24h: float = 0.0
    rain_48h: float = 0.0
    traffic_factor: float = 1.0


class Evidence(BaseModel):
    id: str  # "e#" values, "v#" venue / place names; v0 = the user's location
    label: str
    value: float | str
    unit: str | None = None
    source: SourceKey
    fetched_at: datetime
    note: str | None = None  # e.g. "modelled estimate"


class DegradedSource(BaseModel):
    source: SourceKey
    stage: str
    reason: Literal["timeout", "http", "rate_limited", "empty", "parse", "horizon"]
    detail: str
    impact: str


class Factor(BaseModel):
    code: ReasonCode
    label: str
    delta: float
    gate: bool = False
    evidence_ids: list[str] = []


class ScoredSlot(BaseModel):
    venue_id: str
    start: datetime
    end: datetime
    score: float = Field(ge=0, le=100)
    blocked: bool
    gates: list[Factor] = []
    factors: list[Factor] = []
    pm25_mean: float | None = None
    wbgt_max: float
    rain_prob_max: float
    exposure_mean: float = 0.0
    dose_pct: float | None = None


class AdviceItem(BaseModel):
    code: str
    label: str
    evidence_ids: list[str] = []


class AvoidRange(BaseModel):
    venue_id: str | None = None
    start: datetime
    end: datetime
    reason: ReasonCode
    evidence_ids: list[str] = []


class Advice(BaseModel):
    carry: list[AdviceItem] = []
    watch_for: list[AdviceItem] = []
    avoid_when: list[AvoidRange] = []


class WhyNot(BaseModel):
    venue_id: str
    slot: ScoredSlot
    decisive: Factor


class Recommendation(BaseModel):
    status: Literal["ok", "no_safe_window"]
    best: ScoredSlot
    alt_venue: ScoredSlot | None = None
    alt_time: ScoredSlot | None = None
    why_not: WhyNot | None = None
    avoid: list[AvoidRange] = []
    improves_at: datetime | None = None
    window_label: str
    confidence: float = Field(ge=0, le=1)


class EngineInput(BaseModel):
    """Everything the pure engine needs: fetched data + the resolved request. Serialisable, so
    fine-tuning data can be generated offline by replaying the real engine."""

    parsed: ParsedQuery
    activity: Activity
    place: Place
    radius_km: float
    venues: list[Venue]
    window: TimeWindow
    context: ContextData | None = None
    forecast: Forecast
    air_quality: AirQualityForecast | None = None
    stations: StationSet | None = None
    travel: list[TravelTime] | None = None
    degraded: list[DegradedSource] = []
    now: datetime  # IST, aware


class EngineResult(BaseModel):
    recommendation: Recommendation | None = None  # None: no venue had forecast data
    advice: Advice | None = None
    slots: list[ScoredSlot] = []  # every scored (venue, start) in the window
    hourly: list[HourConditions] = []
    sources: dict[str, list[PollutionSource]] = {}  # venue id -> exposure sources (map)
    venue_refs: dict[str, str] = {}  # venue id -> v# evidence id
    evidence: list[Evidence] = []
    assumptions: list[str] = []
    notes: list[str] = []  # e.g. the medical disclaimer


# ---------------------------------------------------------------- API


class Grounding(BaseModel):
    validator_passed: bool
    fallback_used: bool
    retries: int = 0


class HourCompact(BaseModel):
    venue_id: str
    t: datetime
    score: float | None = None
    pm25_local: float | None = None
    naqi: NaqiCategory | None = None
    wbgt: float
    rain_prob: float
    exposure: float
    wind_kmh: float
    wind_dir_deg: float


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    session_id: str | None = None


class AskResponse(BaseModel):
    query_id: str
    status: Status
    answer_text: str
    recommendation: Recommendation | None = None
    alternatives: list[ScoredSlot] = []
    why_not: WhyNot | None = None
    advice: Advice | None = None
    hourly: list[HourCompact] | list[HourConditions] = []  # debug=1 → full grid
    evidence: list[Evidence] = []
    parsed: ParsedQuery | None = None
    assumptions: list[str] = []
    degraded_sources: list[DegradedSource] = []
    grounding: Grounding
    map_url: str | None = None
    sources: list[str] = []
    attribution: str
    latency_ms: dict[str, int] = {}


class LlmHealth(BaseModel):
    model_loaded: bool
    model_repo: str
    model_file: str
    n_ctx: int
    n_threads: int
    prompt_eval_tok_s: float | None = None
    generation_tok_s: float | None = None
    load_s: float | None = None
    error: str | None = None


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    llm: LlmHealth
