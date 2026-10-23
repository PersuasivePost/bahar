# Bahar — Claude Code prompt pack

## How to run the blitz

**Before you start (10 min):**
- Hugging Face write token: https://huggingface.co/settings/tokens
- WAQI free token: https://aqicn.org/data-platform/token/
- Kaggle account with phone verified (needed for GPU later)
- Create a Hugging Face Space: SDK **Docker**, hardware **CPU basic**, public, name `bahar`

```bash
mkdir bahar && cd bahar && git init
# put CLAUDE.md and PROMPTS.md in this folder
git add -A && git commit -m "brief"
claude
```

**Rules:**
- One step per fresh session: run `/clear`, then paste the next prompt. CLAUDE.md carries the context.
- If a smoke run fails, let Claude Code fix it inside the same step. Stuck for more than 30 min → stop and bring the error to chat.
- Commit after every step: `git add -A && git commit -m "step N"`. From step 1 on, also `git push space main`.
- Reject any scope expansion Claude Code proposes. The answer is "note it, don't build it".

**Checkpoints:**
- **Thu 22 Oct:** steps 0–7 → live endpoint, submittable.
- **Fri 23 Oct:** steps 8–12 → map, failure report, fine-tuned model.
- **Sat 24 Oct:** step 13 only if everything is green, then step 14. **Submit by 8 PM IST.**

---

## Step 0 — Think and plan (plan mode, no code)

**What's happening:** Claude Code reads the whole brief and designs the system before touching code, so we don't rebuild things later. You approve or cut the plan.

Switch Claude Code to plan mode, then paste:

```
Read CLAUDE.md and PROMPTS.md completely. Do not write code yet.
Think hard about the whole system end to end, then give me:
1. The final module layout (adjust CLAUDE.md's only if you have a strong reason).
2. The pydantic models: ParsedQuery, TimeSpec, Profile, Venue, VenueContext, HourConditions,
   Evidence, DegradedSource, Factor, ScoredSlot, Advice, Recommendation, AskResponse.
3. The order of external calls, what runs in parallel, and a latency budget that fits
   p50 ≤ 30 s on a 2-vCPU CPU box including two SLM calls.
4. The top 5 technical risks and how the design handles each.
5. Anything in CLAUDE.md that is wrong, contradictory or underspecified.
Keep it to one screen. Wait for my go.
```

**Test / expect:** A tight plan with models and a latency budget. Push back on anything that adds scope, then reply "go" and run `/clear`.

---

## Step 1 — Scaffold and de-risk hosting first

**What's happening:** We build the skeleton and get the model running on the free Space on day one. If CPU inference is too slow, we find out now, not on Saturday.

```
Step 1 — scaffold + de-risk hosting. Follow CLAUDE.md and the approved plan.
1. uv project (Python 3.11) with the package layout. Settings via pydantic-settings and
   .env.example (WAQI_TOKEN, NOMINATIM_EMAIL, MODEL_REPO, MODEL_FILE, CACHE_DIR,
   PUBLIC_BASE_URL, OPENAQ_API_KEY optional).
2. bahar/models.py with every model from the approved plan.
3. FastAPI app: GET /health (model loaded, prompt-eval and generation tokens/sec from warmup,
   version), POST /ask and GET /ask?q= returning a valid stub AskResponse. Request id +
   structured logging.
4. llm/runtime.py: download the GGUF with hf_hub_download (default
   Qwen/Qwen2.5-1.5B-Instruct-GGUF, Q4_K_M file; verify the exact filename), load with
   llama-cpp-python (n_threads = CPU count, n_ctx 4096), warm up at startup, measure tokens/sec,
   and support reusing a cached static system-prompt prefix between calls.
5. Dockerfile for a Hugging Face Docker Space: python:3.11-slim, prebuilt CPU wheel for
   llama-cpp-python (no source compile), model baked into the image at build time, non-root
   user uid 1000, port 7860. Put the Space front-matter at the top of README.md
   (sdk: docker, app_port: 7860).
Run it locally and show the /health output.
```

**Test / expect:**
```bash
uv run uvicorn bahar.api.main:app --port 7860
curl -s localhost:7860/health
git add -A && git commit -m "step 1"
git remote add space https://huggingface.co/spaces/<hf-user>/bahar
git push space main
curl -s https://<hf-user>-bahar.hf.space/health
```
Expect /health to report the model loaded locally and on the Space. If generation on the Space is below ~5 tokens/sec, stop and bring the numbers to chat: we decide model size or hosting now.

---

## Step 2 — Data layer

**What's happening:** Typed, cached, rate-limited fetchers for every live source, each one failing gracefully instead of crashing. This is the foundation everything else stands on.

```
Step 2 — data layer. Follow CLAUDE.md (rate limits, attribution, never-crash rule).
Implement bahar/data/:
- cache.py: async-friendly TTL cache on diskcache; static (7 d) vs live (15 min) TTL helpers;
  key = normalized request.
- geocode.py: Nominatim search with countrycodes=in; returns lat/lon, display name, bbox and
  place rank (city vs neighbourhood, for radius defaults); 1 req/s limiter.
- overpass.py: mirror rotation (overpass-api.de, overpass.kumi.systems,
  overpass.private.coffee) with backoff.
  (a) Venue query per activity from the CLAUDE.md tag table within the radius
      (`out center tags`) → Venue models (name, osm type/id, centroid, area when available,
      tags, opening_hours, access, lit). Select ≤ 12 candidates: drop unnamed tiny features,
      dedupe by name/proximity, rank by distance and size.
  (b) ONE batched context query for all candidates (≤ 2 km): roads by class, industrial /
      landfill / construction / quarry landuse, fossil power plants, water / coastline /
      river, green landuse, with geometry (`out geom`). Keep the payload small.
- weather.py: Open-Meteo forecast for all candidates in ONE request, timezone Asia/Kolkata,
  past_days=2. Hourly: temperature_2m, relative_humidity_2m, dew_point_2m,
  apparent_temperature, wet_bulb_temperature_2m, precipitation_probability, precipitation,
  weather_code, cloud_cover, wind_speed_10m, wind_direction_10m, wind_gusts_10m, uv_index,
  shortwave_radiation, direct_radiation, cape, is_day. Daily: sunrise, sunset.
  Verify the variable names against the docs and drop any the API rejects.
- airquality.py: Open-Meteo air-quality (pm2_5, pm10, nitrogen_dioxide, ozone), all
  candidates in one request.
- stations.py: WAQI stations in the bounding box with current PM2.5; OpenAQ v3 fallback when
  a key is set. Return coords, value, observed time, source.
- routing.py: OSRM table from origin to all candidates (car: router.project-osrm.org;
  bike/foot: routing.openstreetmap.de). Fallback: haversine × 1.4 / mode speed, flagged as
  an estimate.
- elevation.py: OpenTopoData SRTM 30 m batch lookup (used later for hikes).
Every function returns typed models plus Evidence records (source + fetched_at) and reports
failures as DegradedSource instead of raising.
Add scripts/smoke_data.py "<place>" <activity>: runs the full fetch chain and prints a
compact summary with per-source timings.
```

**Test / expect:**
```bash
uv run python scripts/smoke_data.py "Powai, Mumbai" running
uv run python scripts/smoke_data.py "Lonavala" hiking
```
Expect real named venues (parks for Powai, forts and viewpoints for Lonavala), context feature counts per venue, hourly weather rows, at least one station and travel times. The second run should be much faster from cache.

---

## Step 3 — Profiles and time resolver

**What's happening:** The activity and health tables become one typed source of truth. A deterministic resolver turns phrases like "this weekend" into exact IST hours, so the small model never has to do date maths.

```
Step 3 — profiles + time resolver. Follow CLAUDE.md.
- bahar/config/activities.py, health.py, weights.py: encode the CLAUDE.md tables as typed,
  frozen config objects, plus a synonyms map per activity (English, Hinglish, Hindi in
  Devanagari, Marathi) for normalization.
- bahar/engine/timewindow.py: pure function (TimeSpec, now in IST, optional sunrise/sunset)
  → candidate hourly start times + human label + assumptions. Handle today / tomorrow /
  day_after, weekday names (next occurrence; "Saturday" said on Saturday morning = today),
  this/next weekend, next N days, parts of day, explicit hour, around sunrise/sunset, and the
  default next 24 h. Never return past hours.
- scripts/smoke_time.py: with fixed now = Wed 21 Oct 2026 22:30 IST and again with
  Sat 24 Oct 2026 09:00 IST, print a table for ~20 time specs.
```

**Test / expect:**
```bash
uv run python scripts/smoke_time.py
```
Expect "this weekend" from Wednesday → Sat and Sun daytime, "tomorrow evening" → Thu 16:00–20:00, and no past hours in the Saturday-morning run.

---

## Step 4 — Geospatial downscaling (pillar 1)

**What's happening:** This is the core of Bahar: wind-aware pollution exposure, station-corrected local AQ and microclimate per venue per hour. It's what makes two parks in the same city get different answers.

```
Step 4 — geospatial downscaling. Follow the CLAUDE.md formulas exactly; constants in config.
- engine/geo.py: haversine, initial bearing, local metric projection (pyproj; UTM zone by
  longitude), nearest point on a shapely geometry, area in km², share of a buffer covered by
  polygons.
- engine/exposure.py: per venue, per hour exposure index E(h) with the top-3 contributing
  sources (name/class, distance m, bearing°, compass word) as Evidence.
- engine/microclimate.py: green share and water share within 500 m; near-water flag (≤ 300 m).
- engine/aq.py: CAMS background + station bias with decay + exposure uplift + green bonus →
  local PM2.5/PM10 with low/high band per hour and NAQI category, all as Evidence, flagged
  "modelled estimate".
- scripts/smoke_downscale.py "<place>" <activity>: for the top 4 venues print a next-24 h
  table (wind from°, speed, E, PM2.5 background → local, NAQI, top source), then one
  sentence per venue on its best and worst hour.
```

**Test / expect:**
```bash
uv run python scripts/smoke_downscale.py "Powai, Mumbai" walking
```
Expect a venue near a highway or industrial area to show a high E when the wind blows from that source and a lower E when it shifts. Hand-check one bearing on openstreetmap.org.

---

## Step 5 — Physiology, scoring, recommendation (pillar 2 + decision engine)

**What's happening:** Heat, dose, sun, ground, traffic and the venue × hour scorer come together into a full recommendation, still without the LLM. If this output reads well, the hardest part is done.

```
Step 5 — physiology, sun, ground, traffic, scoring, advice. Pure functions only.
- engine/heat.py: hourly WBGT + flag (cite the method in a comment).
  engine/dose.py: session dose + share of the reference day.
- engine/sun.py (astral): sunrise, sunset, golden/blue hour, sun azimuth + elevation per hour.
  engine/ground.py: past-rain wetness. engine/traffic.py: travel multiplier + road traffic
  factor for exposure.
- engine/scoring.py: slots (venue × start hour covering the activity duration), hard gates,
  then soft score with factor breakdown (reason code, label, delta, evidence ids).
  Apply the strictest matching health profile.
- engine/advice.py: carry / watch_for / avoid_when per CLAUDE.md, each with evidence ids.
- engine/recommend.py: best slot, alternative venue, alternative time, why_not, avoid ranges,
  status ok | no_safe_window (least-bad slot + when it improves).
- bahar/cli.py: `python -m bahar.cli --parsed '<ParsedQuery JSON>'` runs fetch + engine
  (no LLM) and pretty-prints the Recommendation with factor breakdowns and timings.
```

**Test / expect:** Run the CLI for: running near Koregaon Park tomorrow evening; asthma + walking near Powai over the next 2 days; golf on Saturday within 40 km of Andheri; plus one scenario of your own. Expect real golf courses for golf, stricter gates and an inhaler in "carry" for the asthma case, and factor breakdowns that make sense when you read them.

---

## Step 6 — SLM parser (pillar 3, part 1)

**What's happening:** The small model turns messy multilingual questions into strict JSON. Grammar-constrained decoding means it can't produce broken output, and the gates handle out-of-scope and missing-location questions gracefully.

```
Step 6 — SLM parser. Follow the CLAUDE.md ParsedQuery schema.
- llm/parser.py: system prompt with the schema + 8–10 compact few-shot examples (English,
  Hinglish, Hindi, Marathi, typos, out-of-scope, missing location, commute).
  JSON-schema / grammar-constrained generation via llama-cpp-python so the output always
  validates. Post-normalize: activity synonyms → enum, radius defaults by activity and place
  rank, clamp ranges, language detection.
- Gates in pipeline.py: out_of_scope → polite answer listing supported activities (user's
  language); needs_clarification → exactly one question for the missing piece (location, or
  commute start/end); missing time → default window + assumption.
- Cache the system prompt + few-shots as a reusable prefix so each request only evaluates
  the user's query.
- scripts/smoke_parse.py: 30 messy queries (write new ones; do not copy the rulebook
  examples) → table of query | parsed fields | gate | ms. Save the output to eval/.
```

**Test / expect:**
```bash
uv run python scripts/smoke_parse.py
```
Expect 100% valid JSON, most fields right, and a few seconds per parse. Keep the misses: they're exactly what the fine-tune will fix.

---

## Step 7 — Grounded composer and full /ask → SUBMITTABLE

**What's happening:** The model writes the answer using only evidence references, a validator rejects any invented number, and the whole pipeline is wired end to end. After this step and a push, you have a valid submission.

```
Step 7 — grounded composer + full /ask. Follow the CLAUDE.md grounding contract.
- engine/evidence.py: EvidenceStore issuing e#/v# ids; facts builder producing a compact,
  token-cheap facts block (best slot, alternatives, why_not, top factors, advice items,
  assumptions) where every value is referenced by id.
- llm/composer.py: answer in the user's language, structured as recommendation (place + time)
  → why (2–3 reasons) → carry → watch for → avoid → one-line why-not. Every number, time and
  venue as {{e#}} / {{v#}}. ≤ 200 tokens.
- llm/validator.py: strip refs, reject any remaining digit or unknown ref; one retry, then
  llm/templates.py deterministic renderer (English + Hinglish).
- render/text.py: fill refs with formatted value + unit; append footnoted sources and the
  attribution line.
- pipeline.py: wire everything with asyncio.gather and per-stage timeouts, record per-stage
  latency, never raise to the client. /ask returns the full AskResponse; ?debug=1 adds the
  hourly grid.
- hf_model/README.md + hf_model/config.json: model card for the base model as currently used
  (Qwen/Qwen2.5-1.5B-Instruct, GGUF Q4_K_M, our prompts/config, intended use, limitations,
  Apache-2.0), ready to push to a public HF model repo.
```

**Test / expect:**
```bash
curl -s -X POST localhost:7860/ask -H 'content-type: application/json' \
  -d '{"query":"kal subah Powai ke paas walk karni hai, mujhe asthma hai"}' | jq .answer_text
curl -s "localhost:7860/ask?q=tennis+after+work+today+near+hinjewadi" | jq '.status,.grounding,.latency_ms'
git add -A && git commit -m "step 7: end to end" && git push space main
hf upload <hf-user>/bahar-qwen2.5-1.5b ./hf_model . --repo-type model   # older CLI: huggingface-cli upload
```
Expect complete answers in roughly 30 s or less, `validator_passed: true` on most, and every number in the text present in `evidence`. **You are now submittable.**

---

## Step 8 — Map page (the demo screen)

**What's happening:** One shareable page per answer showing venues, pollution sources, the wind arrow and an hour slider. It's what makes "same place, different hour" visible to judges in five seconds.

```
Step 8 — map page. render/map.py + render/templates/map.html (Leaflet from CDN, OSM tiles
with attribution).
- Persist each AskResponse snapshot to CACHE_DIR/maps/<query_id>.json for 24 h;
  GET /map/{id} renders it.
- Show: origin + radius circle; candidate venues coloured by score at the selected hour; the
  recommended venue highlighted; contributing pollution sources (roads / industry / landfill)
  drawn and labelled; wind arrow for the selected hour; an hour slider that recolours venues
  and rotates the arrow; side panel with the answer text and evidence footnotes.
  Mobile-friendly.
- map_url in AskResponse uses PUBLIC_BASE_URL.
```

**Test / expect:** Open the `map_url` from any response. Expect venue colours and the wind arrow to change as you drag across hours, with the pollution sources drawn near the venue that lost.

---

## Step 9 — Query battery (a failure report, not tests)

**What's happening:** 80 fresh, messy queries run against the live endpoint to show what breaks. We triage the list together instead of writing tests.

```
Step 9 — query battery.
- eval/queries.jsonl: 80 new queries (do not copy the rulebook examples): all 10 activities,
  at least 12 Indian cities including smaller ones, ~25% Hinglish/Hindi/Marathi, health
  profiles, vague times, typos, missing location, out-of-scope, commute.
- scripts/battery.py <base_url>: runs them 2 at a time, saves
  eval/results/<timestamp>.jsonl, prints a summary (status counts, p50/p95 latency,
  validator pass rate, fallback rate, degraded sources) and the worst 15 with reasons.
```

**Test / expect:**
```bash
uv run python scripts/battery.py https://<hf-user>-bahar.hf.space
```
Expect a ranked failure list. Bring it to chat and we decide what to fix.

---

## Step 10 — Fine-tuning data

**What's happening:** Programmatic training data, with no LLM-generated text, so it's fully disclosable. The compose data comes from running our real engine offline, so the model learns exactly our reference format.

```
Step 10 — fine-tuning data (programmatic, fully disclosed).
- training/gen_parse.py: slot-filling generator → chat-format JSONL (system, user query,
  assistant = exact target JSON). Pools: activity synonyms in en / Hinglish / hi / mr,
  ~300 Indian localities across ≥ 15 cities, time phrases, radius phrases, health/age
  phrases, preferences, commute phrasing. Noise: typos, no punctuation, lowercase,
  code-mixing, Devanagari. Negatives: out-of-scope, missing location, missing activity.
  6,000 train / 400 val, seeded and balanced.
- training/gen_compose.py: sample synthetic venue contexts + hourly conditions, run OUR
  engine offline → facts block; target = composer-format answer from a paraphrase-rich
  template grammar with refs, en + Hinglish. 3,000 train / 200 val.
- eval/parse_gold.jsonl: draft 100 natural queries, kept separate from the generator pools,
  for me to edit by hand (true held-out set).
- Print 10 random samples from each file.
```

**Test / expect:** Read the samples: expect variety and exactly correct targets. Spend 15 minutes editing `parse_gold.jsonl` yourself, because its score goes on a slide.

---

## Step 11 — Fine-tune on Kaggle

**What's happening:** A QLoRA run on a free T4 with a before/after comparison. The adapter and a merged GGUF get pushed to Hugging Face.

```
Step 11 — training/finetune_kaggle.ipynb.
- Unsloth QLoRA on Qwen/Qwen2.5-1.5B-Instruct (4-bit), LoRA r=16, alpha=32, dropout 0,
  all linear layers, max_seq_len 2048, 2 epochs on mixed parse + compose data, loss on
  assistant tokens only.
- Before/after eval: parse JSON validity + field accuracy (activity, location, time,
  profile, lang, in_scope) on val and parse_gold; compose ref-validity rate on compose val.
  Print a comparison table.
- Push the LoRA adapter to <hf-user>/bahar-qwen2.5-1.5b (must load with
  AutoPeftModelForCausalLM.from_pretrained) and a merged GGUF Q4_K_M to
  <hf-user>/bahar-qwen2.5-1.5b-gguf. HF token from Kaggle Secrets.
- Must run top to bottom on a T4 in about 2 h. Last cell: load the adapter with
  from_pretrained and run one query.
```

**Test / expect:** Upload the notebook and the data as a Kaggle dataset, set Accelerator to GPU T4, add secret `HF_TOKEN`, then Run All. Expect a metrics table where the fine-tuned model beats the base, especially on Hinglish and the gold set.

---

## Step 12 — Model card and swap

**What's happening:** The model card documents everything judges ask for, and the live Space switches to the fine-tuned model. The before/after battery runs become a slide.

```
Step 12 — model card + swap.
- Turn hf_model/README.md into the full model card: base model + licence (Apache-2.0), what
  changed (LoRA tasks), data generation method (programmatic, disclosed), training config,
  eval table base vs fine-tuned, intended use, out-of-scope use, limitations, how to load
  (from_pretrained snippet + GGUF usage). Push it to both model repos.
- Point the Space at the fine-tuned GGUF via MODEL_REPO / MODEL_FILE and rebuild.
```

**Test / expect:** Re-run `scripts/battery.py` against the Space. Expect a higher validator pass rate and fewer parse failures than the step 9 run. Keep both result files.

---

## Step 13 — Extras (only if everything above is green)

**What's happening:** Optional differentiators, each a separate small prompt. Skip any that don't fit the time left.

**13a — Forecast confidence**
```
Fetch precipitation_probability, precipitation and wind from 3 models (ecmwf_ifs025,
gfs_seamless, icon_seamless) via Open-Meteo `models=`; compute the spread per slot; expose it
as a confidence band; the composer mentions it when the spread is large. "Saturday or
Sunday?" questions should get risk ranges for both days.
```
Expect: "Saturday or Sunday" answers show a rain-risk range per day.

**13b — Sunrise/sunset view check (photography)**
```
For photography around sunrise/sunset: cast rays along the sun azimuth ±15° up to 5 km from
each venue; bonus when they cross open water or coastline in the context data. Evidence:
azimuth and what lies in that direction.
```
Expect: sea-facing spots rank above inland parks for sunset queries.

**13c — Follow-ups**
```
session_id keeps the last ParsedQuery in memory (TTL 1 h); the parser receives it, so
"what about Sunday?" merges into the previous query.
```
Expect: a follow-up reuses the earlier location and activity.

**13d — Hikes**
```
OpenTopoData elevation along the line to the hike venue → elevation gain; latest safe start
= sunset − (duration + buffer); add it to watch_for.
```
Expect: hike answers include a "start by" time.

---

## Step 14 — README and slide content

**What's happening:** Judge-facing documentation with full disclosure of sources and tools. The slide content then comes to chat to be built into the deck.

```
Step 14 — README + slide content.
- README.md (keep the Space front-matter at the top): what Bahar is (thesis line), live
  endpoint URL + curl examples, response format, architecture (mermaid), the three pillars,
  model (base + HF links + fine-tune summary), data sources with attribution, tools
  disclosure (built with Claude Code as a coding assistant; the only runtime model is our
  SLM), run locally / Docker, limitations (modelled AQ not measured, no live traffic, OSM
  coverage gaps, CPU latency), licence.
- docs/slides.md: content for 6 slides — problem + thesis, architecture, worked example from
  a real battery result (with the map), model + fine-tune metrics, differentiators, honest
  limitations.
```

**Test / expect:** Read the README once as if you were a judge. Bring `docs/slides.md` to chat and we build the deck.
