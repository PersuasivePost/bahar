---
title: Bahar
emoji: 🌬️
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# Bahar

GeoAI Hackathon @ CSRE, IIT Bombay. Venue-level, hour-level, body-aware outdoor-activity
advice from free open data and a ≤3B open-weights SLM. See `CLAUDE.md` and `docs/plan.md`.

## Run locally

```
cp .env.example .env
uv run uvicorn bahar.api.main:app --port 7860
curl localhost:7860/health
```
