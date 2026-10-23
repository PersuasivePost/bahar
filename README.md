# Bahar — hyperlocal outdoor decision assistant

| | |
|---|---|
| **Live API** | https://home-cloud.tailb8bfd5.ts.net:8443 |
| **Model (Hugging Face)** | https://huggingface.co/persuasivepost/bahar-qwen2.5-1.5b |
| **Code** | https://github.com/PersuasivePost/bahar |

Ask a free-form question about an outdoor activity. Bahar returns the best nearby place and
time, what to carry, what to watch for and when to avoid going, with every number backed by
data fetched for that query.

> Forecasts are ~45 km wide (Open-Meteo's air-quality forecast outside Europe is CAMS Global
> at 0.4°). People live at 500 m. Bahar adds the missing geography, for the body of the person
> asking.

## Try it

```bash
curl -s -G "https://home-cloud.tailb8bfd5.ts.net:8443/ask" \
  --data-urlencode "q=best time for a morning jog near Powai tomorrow, I have asthma"

curl -s -X POST "https://home-cloud.tailb8bfd5.ts.net:8443/ask" \
  -H 'content-type: application/json' \
  -d '{"query":"kal shaam Koregaon Park mein cycling karni hai"}'

curl -s https://home-cloud.tailb8bfd5.ts.net:8443/health
```

- A first request for a place can take 20–40 s (live weather, air quality and routing, plus
  CPU inference). Repeat requests are faster.
- `status` is one of `ok`, `no_safe_window`, `needs_clarification`, `out_of_scope`,
  `no_venues`, `error`.
- Main response fields: `answer_text`, `recommendation`, `alternatives`, `why_not`, `advice`
  (carry / watch_for / avoid_when), `hourly`, `evidence` (every cited value with its source and
  fetch time), `parsed`, `assumptions`, `degraded_sources`, `grounding`, `map_url`, `sources`,
  `attribution`, `latency_ms`.
- `map_url` opens an interactive map: venues coloured by score per hour, wind arrow, and the
  pollution sources around each venue.
- A basic per-IP rate limit (10 requests/minute) protects the endpoint.

## What it does

Activities: running, walking, cycling, golf, cricket, football, tennis, hiking, picnic,
outdoor event, photography. Health and age profiles (asthma/COPD, heart condition, senior,
child, pregnancy) tighten the limits. Questions can be in English, Hinglish, Hindi or Marathi.
Missing location, vague times and out-of-scope requests get a polite answer or one clarifying
question instead of a guess.

### Three ideas

1. **Geospatial downscaling.** For each venue and hour, a pollution *exposure index* combines
   nearby roads, industry, landfill, construction and quarries with the wind: sources count
   when the wind blows from them to the venue, decay with distance, and weigh more when the air
   is calm. Local PM2.5 = CAMS background × ground-station correction (when a fresh station
   exists) × exposure uplift, with an uncertainty band. Water and green cover, recent rain,
   opening hours and travel time (with a rush-hour multiplier) complete the picture.
2. **Personal physiology.** The same air means different things for a runner and a picnic:
   activity-specific breathing rates give an inhaled-dose share; outdoor WBGT (a simplified
   Liljegren model) gates heat stress; limits tighten per health profile.
3. **Grounded by construction.** The small model parses the question and writes the answer, but
   it cannot type a number, time or place name: decoding is grammar-constrained to evidence
   references (`{{e12}}`, `{{v1}}`) that code fills in from the engine's evidence records. A
   validator also rejects lines that cite another section's facts, use clean-air words for poor
   air, or invent content. A rejected line is replaced by a deterministic template line.

### Pipeline

```
question
 → SLM parser (grammar-constrained JSON) + rule layer for days, hours, conditions, places
 → gate: out_of_scope | needs_clarification
 → time resolver (IST hours) → geocode → venues + context (local OSM index)
 → in parallel: weather · air quality · ground stations · routing
 → engine: exposure → local air quality → heat/dose → sun → ground wetness → scoring
           (venue × start hour; hard gates, then soft score with reasons) → advice
 → SLM composer (evidence references only) → validator → renderer (or template fallback)
 → answer + evidence + map
```

## Model

- **Base model:** `Qwen/Qwen2.5-1.5B-Instruct` (Apache-2.0), run as GGUF Q4_K_M
  (`Qwen/Qwen2.5-1.5B-Instruct-GGUF`, `qwen2.5-1.5b-instruct-q4_k_m.gguf`) with
  `llama-cpp-python` on CPU. The only language model in the running assistant.
- **Changes:** none to the weights. We add task prompts, a grammar for the parser output, a
  per-request grammar for the composer (evidence references only, no digits), and a validator.
  <!-- EDIT THIS SECTION if a fine-tuned model ships -->
- **Hugging Face repo:** `persuasivepost/bahar-qwen2.5-1.5b` holds the model card and
  configuration (`hf_model/`).

## Data sources

| Source | Used for | How |
|---|---|---|
| OpenStreetMap, Geofabrik India zone extracts (snapshot 2026-10-22) | venues, roads, industry, landfill, construction, quarries, water, green cover, opening hours | local SQLite index built offline (`scripts/build_osm_index.py`) |
| Overpass API | same features, live fallback outside the index | mirror rotation, circuit breaker, stale cache |
| Nominatim | geocoding, venue fallback | ≤ 1 request/s, contact in User-Agent |
| Open-Meteo Forecast | hourly weather, past 2 days of rain, sunrise/sunset | live per query |
| Open-Meteo Air Quality (CAMS) | background PM2.5/PM10 | live per query |
| OpenAQ, WAQI | ground-station PM2.5 for correction | live; only stations with a reading < 6 h old |
| OSRM public servers | travel times | live; haversine estimate if unavailable |
| OpenTopoData (SRTM) | elevation for hikes | live |
| astral | sun position, golden hour | computed locally |

Attribution: © OpenStreetMap contributors · Weather & air quality: Open-Meteo.com, CAMS
(Copernicus Atmosphere Monitoring Service) · Ground stations: OpenAQ / WAQI and their
originators. Weather, air quality, stations and routing are fetched per query. Map geography is
a local snapshot of an open extract (listed in the rulebook as an allowed source); the live
Overpass API is used outside the indexed area.

## Run it yourself

```bash
git clone https://github.com/PersuasivePost/bahar && cd bahar
uv sync
cp .env.example .env     # set NOMINATIM_EMAIL, WAQI_TOKEN, optional OPENAQ_API_KEY, PUBLIC_BASE_URL

# map data (once)
sudo apt install -y osmium-tool
mkdir -p ~/osm && cd ~/osm
wget -c https://download.geofabrik.de/asia/india/western-zone-latest.osm.pbf   # + other zones you need
cd - && OSM_DIR=$HOME/osm PYTHONPATH=. uv run python scripts/build_osm_index.py

# serve (first start downloads the ~1 GB GGUF; afterwards set HF_HUB_OFFLINE=1)
uv run uvicorn bahar.api.main:app --host 127.0.0.1 --port 7860
```

Startup takes about a minute (model load and prompt-prefix warm-up). We host it on a home
server exposed with Tailscale Funnel (a Cloudflare quick tunnel is the backup). A `Dockerfile`
is included but has not been tested end to end.

## Evaluation

`eval/queries.jsonl` holds 80 messy queries (11 activities, 25 Indian cities, Hinglish/Hindi/
Marathi, typos, bare place names, missing location, out-of-scope). First full run (23 Oct):

- the validator accepted 52 of 53 composed answers and **no answer contained an unfilled
  reference**;
- the recommended venue was in the expected city for 49 of 51;
- 20 of 53 composed answers had at least one line replaced by a template line.

Final run after the later fixes: **<fill from eval/battery_run2.txt>**.

## Limitations

- **Local air quality is a modelled estimate, not a measurement.** CAMS is coarse; the
  exposure uplift is a transparent heuristic; the uncertainty band is shown. Hourly values use
  NAQI categories only as an indication (NAQI is defined on 24 h averages).
- **Ground stations are often stale.** In Mumbai the official CPCB/MPCB monitors had not
  reported for days; we only correct with readings under 6 hours old and say so otherwise.
- **No live traffic.** Travel time is free-flow routing times a documented time-of-day
  multiplier.
- **OSM coverage varies.** Unnamed or unmapped venues are missing; some names are odd. The map
  snapshot is dated.
- **Small-model phrasing.** The 1.5B model sometimes puts a fact in the wrong sentence. The
  validator catches this and substitutes a template line, so answers can read more mechanical.
  Hindi and Marathi answers use a shorter summary mode with template lists.
- **Latency.** CPU inference plus live fetches: roughly 15–25 s warm, up to ~40 s on a cold
  place.
- **Scope gaps.** Commute queries are evaluated around the starting point; WBGT is a simplified
  model; the EPA breakpoint table used to convert WAQI readings may differ from WAQI's current
  one.

## Tools and disclosure

- No hosted LLM runs in the assistant: the only model is Qwen2.5-1.5B-Instruct (GGUF) on our
  own machine.
- Training data (if any) was generated programmatically from templates.

## Licence

Code: MIT. Model: Apache-2.0 (Qwen2.5-1.5B-Instruct).
