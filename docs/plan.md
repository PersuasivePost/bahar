# Bahar — approved design (Step 0)

CLAUDE.md is the spec; this file records the design decisions that implement it: module
layout, models, call order, latency budget, and risks.

## 1. Module layout

As in CLAUDE.md, plus three modules:

- `bahar/data/http.py` — one shared async httpx client: timeouts, retry with exponential
  backoff + jitter, per-host rate limiters (Nominatim, OpenTopoData 1 req/s), Overpass mirror
  rotation, every failure converted into a `DegradedSource`. Without it the seven fetchers
  would each repeat the same code.
- `bahar/engine/access.py` — minimal OSM `opening_hours` parser (`24/7`,
  `Mo-Su 05:00-21:00`, day lists, `sunrise-sunset`); unparseable → open + assumption.
- `bahar/llm/prompts.py` — the static parser and composer prefixes (system prompt +
  few-shots), kept separate so their KV states can be saved and restored.

## 2. Models (`bahar/models.py`, pydantic v2)

Enums: `Activity` (11 keys), `Lang` (en, hinglish, hi, mr), `Part`, `Condition`
(asthma, copd, heart, pregnancy, other), `NaqiCategory`, `HeatFlag`, `ReasonCode`,
`SourceKey`, `Status`.

```
TimeSpec        day: str | None          # today | tomorrow | day_after | weekday:<mon..sun> |
                                          # this_weekend | next_weekend | next_n_days | date:YYYY-MM-DD
                n_days: int | None (1–7)
                part: Part | None
                hour: int | None (0–23)
                around: Literal["sunrise", "sunset"] | None

Profile         age_group: Literal["child", "adult", "senior"] | None
                conditions: list[Condition]
                group_size: int | None

Commute         from_: str (alias "from"), to: str

ParsedQuery     in_scope: bool
                activity: Activity | None, activity_raw: str | None
                location_text: str | None, radius_km: float | None
                time: TimeSpec, profile: Profile, preferences: list[str]
                commute: Commute | None, lang: Lang
                # only in_scope and lang are required in the grammar; nulls are omitted
                # to save generated tokens and filled by defaults

Venue           id: str ("osm:way/123"), name: str, kind: str, lat, lon: float
                area_km2: float | None, distance_km: float
                tags: dict[str, str], opening_hours: str | None, access: str | None
                lit: bool | None, private: bool

PollutionSource cls: str, name: str | None, weight: float, distance_m: float, bearing_deg: float

VenueContext    venue_id: str
                sources: list[PollutionSource]      # grouped by (class, name/ref), top 8
                green_share_500m: float, water_share_500m: float, near_water: bool
                travel_min: float | None, travel_estimated: bool

HourConditions  venue_id: str, t: datetime (IST)
                temp_c, rh, dew_c, wind_kmh, wind_dir_deg, gust_kmh, rain_prob, rain_mm,
                weather_code, cloud, uv, swr, cape, is_day
                pm25_bg, pm10_bg: float | None (None beyond AQ horizon), bias: float
                exposure: float, top_source_ids: list[str]
                pm25_local, pm25_low, pm25_high, pm10_local: float | None
                naqi: NaqiCategory | None
                wbgt: float, heat_flag: HeatFlag
                sun_elev, sun_az: float, light: Literal["day", "golden", "blue", "dark"]
                rain_24h, rain_48h: float, traffic_factor: float

Evidence        id: str ("e#" values, "v#" venue/place names; v0 = user location)
                label: str, value: float | str, unit: str | None
                source: SourceKey, fetched_at: datetime, note: str | None ("modelled estimate")

DegradedSource  source: SourceKey, stage: str
                reason: Literal["timeout", "http", "rate_limited", "empty", "parse", "horizon"]
                detail: str, impact: str

Factor          code: ReasonCode, label: str, delta: float, gate: bool, evidence_ids: list[str]

ScoredSlot      venue_id: str, start, end: datetime, score: float (0–100), blocked: bool
                gates: list[Factor], factors: list[Factor]
                pm25_mean: float | None, wbgt_max: float, rain_prob_max: float, dose_pct: float | None

AdviceItem      code: str, label: str, evidence_ids: list[str]
AvoidRange      venue_id: str | None, start, end: datetime, reason: ReasonCode, evidence_ids
Advice          carry: list[AdviceItem], watch_for: list[AdviceItem], avoid_when: list[AvoidRange]

WhyNot          venue_id: str, slot: ScoredSlot, decisive: Factor

Recommendation  status: Literal["ok", "no_safe_window"]
                best: ScoredSlot, alt_venue: ScoredSlot | None, alt_time: ScoredSlot | None
                why_not: WhyNot | None, avoid: list[AvoidRange]
                improves_at: datetime | None, window_label: str, confidence: float

Grounding       validator_passed: bool, fallback_used: bool, retries: int

HourCompact     venue_id, t, score, pm25_local, naqi, wbgt, rain_prob, exposure, wind_kmh, wind_dir_deg

AskResponse     query_id: str
                status: Literal["ok", "no_safe_window", "needs_clarification", "out_of_scope", "no_venues"]
                answer_text: str, recommendation: Recommendation | None
                alternatives: list[ScoredSlot], why_not: WhyNot | None, advice: Advice | None
                hourly: list[HourCompact] (debug=1 → full HourConditions grid)
                evidence: list[Evidence], parsed: ParsedQuery | None
                assumptions: list[str], degraded_sources: list[DegradedSource]
                grounding: Grounding, map_url: str | None
                sources: list[str], attribution: str, latency_ms: dict[str, int]
```

## 3. Call order, parallelism, latency budget

```
parse (SLM #1, parser prefix KV restored)
  → gates (out_of_scope / needs_clarification) → timewindow (pure)
  → geocode (Nominatim; 2 calls ≥ 1 s apart for commute)
  → Overpass venues → select ≤ 12 candidates (commute: 2 pseudo-venues)
  → asyncio.gather:
       Overpass context (one query around all candidates, roads motorway–secondary only)
       Open-Meteo forecast (all candidates, past_days=2)
       Open-Meteo air quality (all candidates)
       WAQI bbox → ≤ 3 station feeds (OpenAQ fallback)
       OSRM table (origin → candidates)
  → engine (pure, geometries projected once per query)
  → compose (SLM #2, composer prefix KV restored) → validate → render / template
```

Budget with the static cache warm (geocode + OSM within 7 days):

| stage | mode | budget |
|---|---|---|
| parser: ~40 query tokens to evaluate, ~60 output tokens | serial | 6 s |
| geocode | serial | 1 s |
| Overpass venues | serial | 3 s (timeout 12 s) |
| context + weather + AQ + stations + OSRM | parallel | 5 s warm (context cold: 12 s attempt on the last-good mirror + one retry on another, 20 s total, then degrade) |
| engine (12 venues × ≤ 48 h) | CPU | 1 s |
| composer: ≤ 350-token facts block at ~80 tok/s, ≤ 160 output tokens at ~12 tok/s | serial | 14 s |
| validate + render + overhead | — | 1 s |
| **total** | | **≈ 31 s** |

- The budget holds only if generation runs at ≥ 12 tok/s; Step 1 measures this on the Space.
- If generation is < 8 tok/s, the default composer mode becomes `summary` (SLM writes
  recommendation + why, ~70 tokens; lists come from templates), which saves ~8 s.
- The retry is deadline-aware: a validator failure with < 15 s left goes straight to the
  template renderer.
- A cold static cache adds 5–15 s (Overpass), so p50 is reported on the warm path.

## 4. Top risks

1. **CPU SLM latency.** llama-cpp-python reuses only the previous prompt's prefix, so
   alternating parser/composer calls would re-evaluate both prefixes every time. Mitigation:
   at startup, evaluate each prefix once, `save_state()`, and `load_state()` before each
   call (Qwen2.5-1.5B KV ≈ 28 KB/token → ~40 MB per prefix). Optional JSON fields,
   short refs, output caps, `summary` composer mode.
2. **Overpass flakiness / payload size.** Mitigation: minimal tag filters (roads
   motorway–secondary only), one union query around all candidates with `out geom`, mirror
   rotation + backoff, 7-day cache. Context failure → E = 0, lower confidence, reported in
   `degraded_sources`. Venue-query failure or no venues → `no_venues`.
3. **Station fusion units and quality.** WAQI values are US-EPA AQI sub-indices. Mitigation:
   invert the EPA PM2.5 breakpoints to µg/m³, drop readings older than 6 h, decay the bias from the observation time, clamp bias to
   [0.5, 2.5] per spec.
4. **Grounding leaks.** The SLM can write unreferenced names or number words. Mitigation:
   validator rejects Unicode digits (incl. Devanagari), number words from a small en/hi/mr
   list only when adjacent to a unit or time word, and any venue/place name from this
   query's data written outside a ref. The user's location is `v0`.
5. **HF Space runtime.** `os.cpu_count()` reports host cores → thread oversubscription;
   a wheel built for the wrong CPU features crashes at load. Mitigation: threads from
   `os.sched_getaffinity` / cgroup quota; generic AVX2 prebuilt CPU wheel; model baked into
   the image.

## 5. Resolved spec questions (now in CLAUDE.md)

- 11 activities (10 required + walking); the battery covers all 11.
- Parts of day are sets of start hours; evening = 16–19.
- Road traffic factor: peak 1.3 = hours with a 1.7 travel multiplier; night 0.6 = 23–06.
- Exposure sources grouped by (class, name/ref), nearest point per group, top 8.
- WAQI AQI → µg/m³ via inverted EPA breakpoints; readings older than 6 h dropped; bias decays from observation time.
- Hours beyond the AQ horizon → AQ unknown + degraded.
- AQ gate = session mean local PM2.5; WBGT gate = slot maximum hour.
- Full AskResponse status list: ok, no_safe_window, needs_clarification, out_of_scope, no_venues.
- Closed `conditions` enum ending in `other`.
- Template fallback: hi → Hinglish, mr → English, recorded as an assumption.
- Commute: origin + destination as two pseudo-venues, departure time varies; a missing
  endpoint → needs_clarification. Route sampling deferred to Step 13.
- Distances from the venue centroid: documented limitation.
