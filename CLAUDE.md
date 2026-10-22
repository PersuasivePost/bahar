# CLAUDE.md — Bahar

## Mission — read first, do not drift

We are building **Bahar** for the GeoAI Hackathon @ CSRE, IIT Bombay.
**Submission deadline: Saturday 24 Oct 2026, 23:59 IST.**

Bahar is an HTTP API. A user asks a free-form question about an outdoor activity
("asthma hai, kal subah Powai ke paas walk kahan karu?"). Bahar returns the best nearby
place and time, what to carry, what to watch for, and when to avoid going. Every claim is
backed by data fetched live for that query. The language layer is a ≤3B open-weights SLM
that we run ourselves.

**Thesis:** forecasts are ~45 km wide (Open-Meteo air quality for India comes from CAMS
Global at 0.4°). People live at 500 m. Bahar adds the missing geography, for the body of
the person asking.

Everything we build serves one of three pillars:

1. **Geospatial downscaling** — turn coarse forecasts into venue-level, hour-level
   conditions: wind-aware pollution-source exposure, ground-station AQ fusion,
   microclimate (green/water), ground condition (recent rain), venue reality
   (opening hours, access).
2. **Personal physiology** — the same air and heat mean different things for a runner vs a
   picnic, and for an asthmatic, child, senior or pregnant user: intensity-aware AQ limits,
   inhaled-dose share, WBGT heat stress.
3. **Grounded-by-construction multilingual SLM** — the SLM parses messy
   English/Hinglish/Hindi/Marathi questions and writes the answer, but never writes a
   number, time or place name itself: it emits evidence references that code fills in.

**Out of scope — do not build:** auth/accounts, database servers, a frontend app (beyond
the single map page), unit tests / test frameworks / CI, activities beyond the table below,
anything not in PROMPTS.md. If you believe something else is needed, say so in one line and
continue without building it.

## Hackathon rules (non-negotiable)

- Runtime language model: only our ≤3B open-weights SLM. No GPT/Claude/Gemini or any
  hosted LLM anywhere in the running assistant.
- Data: only free, open sources, each disclosed in the README. No paid APIs, no scraping
  behind logins.
- Never hard-code answers, places or the rulebook's example queries. Facts come from fetched
  data, never from model memory.
- Weather and air quality are fetched live per query (cache TTL ≤ 15 min). Static geography
  (OSM features, geocodes) may be cached for 7 days.
- Attribution in every response and on the map: "© OpenStreetMap contributors",
  "Weather & air quality: Open-Meteo.com, CAMS (Copernicus)", plus WAQI / station
  originators when used.
- Rate limits: Nominatim ≤ 1 req/s with a descriptive User-Agent including a contact email;
  Overpass: rotate mirrors, exponential backoff, server-side timeout ≤ 25 s; OpenTopoData
  ≤ 1 req/s and ≤ 100 points per request; OSRM public servers: batch with the table service.

## Working style — blitzkrieg, but clean

- **Think before coding.** For each step: read the files involved, write a plan of
  ≤ 10 bullets, then implement. If this file is wrong or underspecified, say so and propose
  the fix.
- **No unit tests, no test frameworks, no CI.** Each step ends with the smoke command from
  PROMPTS.md. Run it, show the output, list known gaps, stop. Do not start the next step.
- **Code quality still matters:** Python 3.11, full type hints, pydantic v2 models at every
  boundary, small focused modules, clear names, no dead code, no speculative abstraction.
  Comment non-obvious formulas and cite the source for every threshold.
- **The engine is pure.** Everything under `bahar/engine/` is a pure function of
  already-fetched data: no network, no clock reads (`now` is passed in). This lets us
  generate fine-tuning data offline by running the real engine.
- **Never crash /ask.** Every external call: async httpx, timeout, retry with backoff,
  typed result. A failed source lowers confidence and is reported in `degraded_sources`;
  it does not raise.
- **Evidence or it didn't happen.** Every number, time and place name shown to the user maps
  to an `Evidence` record (id, label, value, unit, source, fetched_at).
- **Config as data.** All thresholds, weights and profiles live in `bahar/config/`.
  No magic numbers in engine code.
- At the end of each step, list the files changed.

## Stack

Python 3.11 · uv · FastAPI + uvicorn · httpx (async) · pydantic v2 + pydantic-settings ·
shapely 2 + pyproj · astral · diskcache · llama-cpp-python (GGUF, CPU) ·
Jinja2 + Leaflet (CDN) for the map page.

- **Model:** Qwen2.5-1.5B-Instruct (Apache-2.0 — note Qwen2.5-3B is *not* Apache-licensed,
  so we avoid it), served as GGUF Q4_K_M. Model repo/file come from env vars so the
  fine-tuned version swaps in without code changes.
- **Hosting:** Hugging Face Docker Space, free CPU basic (2 vCPU, 16 GB), port 7860.
- **Fine-tuning:** Unsloth (or TRL + PEFT) QLoRA on a Kaggle T4.

## Architecture

```
query
 → llm.parser (grammar-constrained JSON → ParsedQuery)
     → gate: out_of_scope | needs_clarification
 → engine.timewindow (deterministic: time spec → IST hourly windows)
 → data.geocode (Nominatim, India bias)
 → data.overpass venues (activity tag table) → select ≤ 12 candidates
 → in parallel:
     data.overpass context (≤ 2 km around candidates, ONE batched query)
     data.weather   (Open-Meteo forecast, all candidates in one call, past_days=2)
     data.airquality(Open-Meteo CAMS, all candidates in one call)
     data.stations  (WAQI; OpenAQ fallback)
     data.routing   (OSRM table)
 → engine: exposure → aq → microclimate → heat → dose → sun → ground → traffic
           → scoring (venue × start-hour slots) → advice → recommend
 → llm.composer (refs only) → validator → renderer (or template fallback)
 → AskResponse (+ map page)
```

```
bahar/
  api/main.py            FastAPI: GET /health, POST /ask, GET /ask?q=, GET /map/{id}
  pipeline.py            orchestration, parallelism, per-stage timeouts, latency breakdown
  models.py              all pydantic models
  cli.py                 run fetch + engine without the LLM
  config/settings.py     env settings
  config/activities.py   activity profiles + multilingual synonyms
  config/health.py       health / age profiles
  config/weights.py      exposure weights, thresholds, traffic multipliers
  data/                  http (shared client: retry/backoff, per-host rate limits, mirror
                         rotation, failure → DegradedSource), cache, geocode, overpass, weather,
                         airquality, stations, routing, elevation
  engine/                geo, timewindow, exposure, aq, microclimate, heat, dose, sun,
                         ground, traffic, access (opening_hours), scoring, advice, recommend,
                         evidence
  llm/                   runtime, prompts (static parser/composer prefixes), parser, composer,
                         validator, templates
  render/                text.py, map.py, templates/map.html
scripts/                 smoke_*.py, battery.py
training/                gen_parse.py, gen_compose.py, finetune_kaggle.ipynb
eval/                    queries.jsonl, parse_gold.jsonl, results/
hf_model/                model card + config for the HF model repo
```

## Domain spec

All values below are documented heuristic defaults. Tune them in `bahar/config/`, keep a
comment with the reasoning or source.

### Activities

11 activities (the 10 required + walking). The query battery covers all 11.

| key | OSM venues (Overpass) | radius km | duration min | intensity | ventilation L/min | wind max sustained/gust km/h | rain-prob max % | light |
|---|---|---|---|---|---|---|---|---|
| running | leisure=park/garden/track/nature_reserve, landuse=recreation_ground, natural=beach | 5 | 45 | high | 55 | 35/50 | 40 | dusk ok (+30 min, reflective gear) |
| walking | same as running | 5 | 45 | low | 20 | 40/60 | 40 | dusk ok |
| cycling | large parks, highway=cycleway, landuse=recreation_ground; commute A→B (see ParsedQuery) | 10 | 60 | moderate | 35 | 25/40 | 30 | dusk ok (lights) |
| golf | leisure=golf_course (flag access=private/members) | 40 | 240 | low | 20 | 25/40 | 30 | daylight |
| cricket | leisure=pitch/stadium + sport=cricket, landuse=recreation_ground | 10 | 180 | moderate | 25 | 30/45 | 30 | daylight |
| football | leisure=pitch/stadium + sport=soccer (incl. turfs) | 8 | 90 | high | 50 | 35/50 | 40 | daylight or lit=yes |
| tennis | leisure=pitch + sport=tennis, not indoor | 8 | 90 | high | 45 | 20/30 | 30 | daylight or lit=yes |
| hiking | leisure=nature_reserve, boundary=protected_area/national_park, natural=peak, historic=fort, tourism=viewpoint | 40 | 240 | moderate | 35 | 40/60 | 30 | daylight, back before sunset |
| picnic | leisure=park/garden, tourism=picnic_site, natural=beach, landuse=recreation_ground | 15 | 180 | low | 12 | 30/45 | 25 | daylight |
| event | large leisure=park/garden, landuse=recreation_ground, amenity=events_venue | 15 | 240 | low | 10 | 30/45 | 20 | evening ok |
| photography | tourism=viewpoint, natural=beach, historic=*, man_made=pier/lighthouse, leisure=park | 10 | 90 | low | 12 | 40/60 | 50 | golden/blue hour bonus |

- If the geocoded place is a whole city (from Nominatim place rank), centre on it and use
  max(activity radius, 12 km). Cap any radius at 50 km.
- Ventilation rates are approximate adult values aligned with the US EPA Exposure Factors
  Handbook activity levels.
- Parts of day (IST) are sets of **start hours** (inclusive; the session may run past the
  window end): early_morning 05–07, morning 06–10, midday 11–14, afternoon 12–16,
  evening 16–19, night 19–22. No time given → next 24 h, recorded as an assumption.

### Health / age profiles (strictest applies when several match)

| profile | AQ limit: local PM2.5 µg/m³ (normal / high-intensity) | WBGT stop °C | extras |
|---|---|---|---|
| default adult | 90 / 60 | 32 (high intensity 30) | — |
| asthma / COPD | 60 / 45 | 30 | inhaler, warm-up, avoid near-road hours |
| heart condition | 60 / 45 | 28 | avoid high intensity in heat |
| senior (60+) | 60 / 45 | 28 | prefer flat, shaded, close venues |
| child (< 12) | 60 / 45 | 28 | UV threshold 6, shorter sessions |
| pregnancy | 60 / 45 | 28 | avoid high intensity in heat, hydration |

Any health profile adds: "Not medical advice — follow your doctor's guidance."
Condition `other` (unrecognised condition) applies the asthma/COPD limits as the cautious
default and is recorded as an assumption.

### Exposure index (signature feature)

Sources within 2 km of a venue, with weights w: highway motorway 1.0, trunk 0.8,
primary 0.5, secondary 0.25; landuse=industrial 0.9 × min(1, √area_km²);
landuse=landfill 1.0; landuse=construction 0.35 (OSM construction tags are often
stale); landuse=quarry 0.6; power=plant with a
fossil source 1.0.

Group candidate features by (class, name or ref) so one road split into many OSM ways counts
once; unnamed features group by class + OSM id. For each group, take the nearest point to the
venue: distance d (m) and bearing β (venue → source). Keep the 8 groups with the highest
w·exp(−d/400), after a corridor dedupe: walking the groups by that score, drop a group whose
nearest point lies within 60 m of an already-kept group of the same class (separately
mapped carriageways, unnamed slip roads, twin names for one corridor such as JVLR /
Adi Sankaracharya Marg). The higher-weight group survives (ties: higher score); a dropped
group's different name is recorded as an alias in its Evidence. Distances are measured from the venue centroid — a documented limitation for
very large venues (e.g. Aarey). For hour h with wind-from direction θ and speed u (m/s):

- upwind alignment a = exp(−Δ² / (2·30²)), Δ = smallest angle between β and θ (degrees)
- calm-wind blend (no step at 1 m/s): blend = clamp((u − 0.5) / 1.5, 0, 1);
  a = blend·a_directional + (1 − blend)·0.5; s = blend·clamp(3 / (u + 2), 0.4, 1.0) +
  (1 − blend)·1.3. Missing wind → blend 0. Venue centroid inside the source → a = 1.
- distance decay k = exp(−d / 400) (near-road pollution gradients largely fade within a few
  hundred metres)
- hourly activity factor f: roads — peak 1.3 = hours whose travel multiplier is 1.7 (see
  Travel), night 0.6 = 23:00–06:00, otherwise 1.0; construction — 1.0 for 08:00–20:00,
  0.3 otherwise (no dust at night); other sources 1.0
- E_raw(h) = Σ w·a·k·s·f; E(h) = tanh(E_raw) ∈ [0, 1)

Keep the top 3 contributors per hour (name/class, distance, bearing, compass word) as
Evidence.

### Air quality fusion

- Background: Open-Meteo CAMS pm2_5 and pm10 at venue coordinates (Open-Meteo already
  returns hourly values). Hours beyond the AQ forecast horizon → AQ "unknown" for those
  hours (no AQ gate, no dose) and `airquality` listed in `degraded_sources`.
- Station bias: nearest ≤ 3 stations within 15 km (WAQI; OpenAQ fallback), IDW-averaged
  current PM2.5. WAQI reports US-EPA AQI sub-indices, not µg/m³: convert by inverting the
  US EPA PM2.5 AQI breakpoints. Drop readings observed more than 6 h ago. bias₀ = clamp(station_obs / cams_at_obs_hour, 0.5, 2.5);
  bias(h) = 1 + (bias₀ − 1)·exp(−(h − t_obs) / 12 h), t_obs = IDW-weighted mean observation
  time (the decay starts at the observation, not at now). No stations → bias 1, wider band.
- Local: PM_local(h) = PM_bg(h) · bias(h) · (1 + 0.35·E(h)), minus a green bonus of at most
  5% when green share within 500 m > 40%.
- Band: ±(20% + 10% per 24 h ahead), +15% when no station was available.
- NAQI (CPCB) PM2.5: 0–30 Good, 31–60 Satisfactory, 61–90 Moderate, 91–120 Poor,
  121–250 Very Poor, > 250 Severe. PM10: 0–50, 51–100, 101–250, 251–350, 351–430, > 430.
  NAQI is defined on 24-h averages; label hourly values "indicative".
- This is a modelled estimate, not a measurement. Say so in responses and the README.

### Inhaled dose

dose_µg = PM_local (µg/m³) × ventilation (L/min) / 1000 × duration (min).
Reference day = 60 µg/m³ × 8 L/min × 1440 min / 1000 ≈ 691 µg. Report as
"this session ≈ X% of a full day breathing 'satisfactory' air at rest". Soft score and
explanation only, not a hard gate.

### Heat

Outdoor WBGT from temperature, humidity/dew point, wind and shortwave radiation
(Open-Meteo). The WBGT gate uses the slot's maximum hourly WBGT. Prefer ECMWF `thermofeel` if its WBGT function fits; otherwise a Liljegren-style
or documented simplified approximation, cited in code. Flags: < 23 low, 23–28 moderate,
28–30 high, 30–32 very high, ≥ 32 extreme. Gates per profile table. UV penalties at ≥ 8
(child ≥ 6). Open-Meteo already corrects temperature for elevation; don't double count.

### Weather gates

- Thunderstorm: weather_code 95/96/99 in or adjacent to the slot → block golf, cricket,
  football, tennis, hiking, event, picnic; heavy penalty for the rest.
  CAPE ≥ 1500 J/kg with precipitation probability ≥ 40% → "thunderstorm risk" warning.
- Rain: probability above the activity max → penalty; ≥ 1 mm/h → block tennis, cricket,
  event; ≥ 2.5 mm/h → block everything else.
- Wind: above the activity's sustained/gust limit → block; above 80% of it → penalty.

### Ground condition

Past precipitation (Open-Meteo past_days): ≥ 10 mm in 24 h → "ground likely wet" penalty
for cricket, football, golf, picnic; ≥ 25 mm → block cricket. Hiking: ≥ 15 mm in 48 h →
slippery-trail warning.

### Sun

astral: sunrise, sunset, golden hour (sun elevation −4° to 6°), blue hour, sun azimuth and
elevation per hour. Photography: golden-hour bonus; cloud cover 20–60% around sunset gets a
small bonus.

### Travel and traffic (no free live traffic exists — disclosed heuristic)

OSRM free-flow duration × multiplier: weekdays 08–11 and 17–21 → 1.7; 07–08, 11–12, 16–17,
21–22 → 1.3; weekend daytime → 1.2; otherwise 1.0. Penalty when travel time > 0.5 × activity
duration; venues outside the radius are excluded.

### Scoring

Slot = (venue, start hour) covering the activity duration. Hard gates first (thunderstorm,
rain, wind, darkness, AQ limit, WBGT limit, closed per opening_hours, outside radius).
AQ gate = session mean of PM_local (central estimate) vs the profile limit; WBGT gate = slot
maximum hour. opening_hours: parse common forms (`24/7`, `Mo-Su 05:00-21:00`,
`sunrise-sunset`); unparseable → treat as open and record an assumption.
Then soft score 0–100 = 100 − Σ penalties + Σ bonuses; every factor carries a reason code,
a short label, its delta and the evidence ids it used.

Output: best slot; best alternative at a different venue; best alternative at a different
time; one "why not" (a nearby or well-known venue that lost, with the decisive factor);
avoid ranges (merged blocked hours with their dominant reason); recommendation status
`ok | no_safe_window` (the latter returns the least-bad slot and when conditions improve).

### Advice rules (deterministic; the SLM only phrases them)

- carry: water (WBGT ≥ 25 or duration ≥ 60 min), electrolytes (WBGT ≥ 28),
  sunscreen + cap (UV ≥ 6), rain jacket/umbrella (rain probability 30–60%),
  inhaler (asthma/COPD), N95 (PM_local ≥ 60 with a sensitive profile, or high exposure),
  light/reflective gear (after sunset), insect repellent (water or green near dusk),
  grip shoes (wet trail).
- watch_for: wind-shift times, heat rising after a given hour, storm risk, wet ground,
  rush-hour window.
- avoid_when: merged blocked ranges.

### Grounding contract

- EvidenceStore assigns `e1…eN` (values) and `v1…vN` (venue names).
- The composer prompt has a compact facts block and the user's language. The SLM must write
  every number, time and venue name as `{{eN}}` / `{{vN}}`.
- The user's own geocoded location is evidence `v0`.
- Validator: after removing refs, every ref must exist, and reject: any Unicode digit
  (incl. Devanagari); a number word (small en/hi/mr list) **only when adjacent to a unit**
  (km, min, %, °, AQI, hours/ghante/तास…) **or a time word** (am/pm, baje, वाजता…) — so
  "one of the best" or Hindi "ek" pass; any venue or place name from this query's data
  written outside a ref.
- On failure: one retry if the remaining latency budget allows (≥ 15 s), else straight to the
  deterministic template renderer. Templates exist in English and Hinglish: `hi` → Hinglish
  template, `mr` → English template, recorded as an assumption.
- Composer modes: `full` (SLM writes the whole answer) or `summary` (SLM writes the
  recommendation + why; carry / watch_for / avoid lists come from templates). If Step 1
  measures generation < 8 tok/s on the Space, `summary` is the default.
- Renderer replaces refs with formatted value + unit. The response includes the evidence
  list with sources and fetched_at.

### ParsedQuery (parser output)

```
{ in_scope: bool,
  activity: enum | null, activity_raw: str | null,
  location_text: str | null, radius_km: float | null,
  time: { day: today | tomorrow | day_after | weekday:<mon..sun> | this_weekend |
               next_weekend | next_n_days | date:YYYY-MM-DD | null,
          n_days: int | null,
          part: early_morning | morning | midday | afternoon | evening | night | any | null,
          hour: 0–23 | null, around: sunrise | sunset | null },
  profile: { age_group: child | adult | senior | null,
             conditions: [asthma | copd | heart | pregnancy | other], group_size: int | null },
  preferences: [str],
  commute: { from: str, to: str } | null,
  lang: en | hinglish | hi | mr }
```

Gates: not in scope → `out_of_scope` (polite, lists supported activities). Missing location
and no commute endpoints → `needs_clarification` with exactly one question in the user's
language. Commute with only one endpoint → `needs_clarification` for the missing one.
Missing time → default window + assumption.

Commute (minimal): origin and destination are geocoded and evaluated as two pseudo-venues;
only the departure time varies, and the slot is scored on the worse of the two ends. Full
route sampling is deferred to Step 13.

### API contract

- `POST /ask {"query": str, "session_id"?: str}` and `GET /ask?q=...` (`&debug=1` adds the
  full hourly grid).
- AskResponse: query_id, status (`ok | no_safe_window | needs_clarification | out_of_scope |
  no_venues`), answer_text (markdown, user's language), recommendation,
  alternatives, why_not, advice {carry, watch_for, avoid_when}, hourly (compact per venue per
  hour: score, pm25_local, naqi, wbgt, rain_prob, exposure, wind), evidence[], parsed,
  assumptions[], degraded_sources[], grounding {validator_passed, fallback_used, retries}, map_url,
  sources[], attribution, latency_ms {stage: ms}.
- Latency target: p50 ≤ 30 s on the free 2-vCPU Space (budget in docs/plan.md). Parser and
  composer have different static prefixes, so save each prefix's KV state at startup and
  restore it before each call (llama-cpp-python only reuses the previous prompt's prefix).
  Keep generation ≤ 220 tokens. Thread count from the CPU affinity / cgroup quota, not
  `os.cpu_count()`.

### Data sources (disclose all)

Nominatim (geocoding) · Overpass API / OpenStreetMap (venues, roads, industry, water, green)
· Open-Meteo Forecast (hourly weather, past 2 days of rain) · Open-Meteo Air Quality (CAMS)
· WAQI (ground stations incl. CPCB; free token) · OpenAQ (fallback) · OSRM public routing
(router.project-osrm.org for car; routing.openstreetmap.de for bike/foot) · OpenTopoData
(SRTM elevation, hikes) · astral (sun position, computed locally).
