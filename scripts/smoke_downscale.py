"""Geospatial downscaling for a place + activity: per-venue exposure, microclimate and local AQ
for the next 24 h, without the LLM.

    uv run python scripts/smoke_downscale.py "Powai, Mumbai" walking
"""

import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # no installed package

from bahar.config.activities import ACTIVITIES  # noqa: E402
from bahar.config.sources import IST  # noqa: E402
from bahar.data.airquality import fetch_air_quality  # noqa: E402
from bahar.data.geocode import geocode, search_radius_km  # noqa: E402
from bahar.data.http import close_client  # noqa: E402
from bahar.data.overpass import fetch_context, fetch_venues  # noqa: E402
from bahar.data.stations import fetch_stations  # noqa: E402
from bahar.data.weather import fetch_forecast  # noqa: E402
from bahar.engine.aq import air_quality_assumptions, local_air_quality, station_bias  # noqa: E402
from bahar.engine.evidence import EvidenceStore  # noqa: E402
from bahar.engine.exposure import describe, hourly_exposure, pollution_sources  # noqa: E402
from bahar.engine.geo import FeatureIndex, LocalFrame, compass_word  # noqa: E402
from bahar.engine.microclimate import microclimate  # noqa: E402
from bahar.models import Activity, DegradedSource  # noqa: E402

TOP_VENUES = 4
HOURS = 24


def fetch_status(name: str, fetch, elapsed_ms: int) -> str:  # type: ignore[no-untyped-def]
    state = "cache" if fetch.cached else ("live" if fetch.value is not None else "FAILED")
    return f"{name} {state} ({elapsed_ms} ms)"


async def main(place_text: str, activity: Activity) -> None:
    degraded: list[DegradedSource] = []
    g = await geocode(place_text)
    place = g.value
    if place is None:
        print("geocode failed:", g.degraded)
        return
    radius = search_radius_km(place, ACTIVITIES[activity].radius_km)
    vf = await fetch_venues(activity, place.lat, place.lon, radius)
    venues = vf.value
    degraded += g.degraded + vf.degraded
    if not venues:
        print("no venues;", degraded)
        return
    now = datetime.now(IST)
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    until = start + timedelta(hours=HOURS - 1)

    started = time.perf_counter()
    ctx_f, fc_f, aq_f, st_f = await asyncio.gather(
        fetch_context(venues), fetch_forecast(venues),
        fetch_air_quality(venues, need_until=until), fetch_stations(venues),
    )  # fmt: skip
    parallel_ms = int((time.perf_counter() - started) * 1000)
    for f in (ctx_f, fc_f, aq_f, st_f):
        degraded += f.degraded
    if fc_f.value is None:
        print("no forecast;", degraded)
        return

    t0 = time.perf_counter()
    store = EvidenceStore()
    features = ctx_f.value.features if ctx_f.value else []
    ctx_at = ctx_f.value.fetched_at if ctx_f.value else fc_f.value.fetched_at
    index = FeatureIndex(LocalFrame(place.lat, place.lon), features)
    results = []
    for v in venues:
        sources = pollution_sources(v, index, store, ctx_at)
        micro = microclimate(v, index, store, ctx_at)
        hours = [h for h in fc_f.value.venues[v.id].hours if start <= h.t <= until]
        exposure = hourly_exposure(v, sources, hours, store, fc_f.value.fetched_at)
        cams = aq_f.value.venues.get(v.id) if aq_f.value else None
        bias = station_bias(v, st_f.value, cams, store)
        aq = local_air_quality(
            v, exposure, cams, bias, micro.green_share, now, store,
            aq_f.value.fetched_at if aq_f.value else now,
        )  # fmt: skip
        results.append((v, sources, micro, hours, exposure, bias, aq))
    engine_ms = int((time.perf_counter() - t0) * 1000)

    print(f"place  {place.name[:90]}\n       ({place.lat:.4f}, {place.lon:.4f}), radius {radius:g} km, "
          f"{len(venues)} candidates; window {start:%a %H:%M} – {until:%a %H:%M} IST")
    print(f"fetch  {fetch_status('context', ctx_f, ctx_f.elapsed_ms)} · "
          f"{fetch_status('forecast', fc_f, fc_f.elapsed_ms)} · {fetch_status('aq', aq_f, aq_f.elapsed_ms)} · "
          f"{fetch_status('stations', st_f, st_f.elapsed_ms)} · parallel wall {parallel_ms} ms")
    print(f"       {len(features)} context features; engine {engine_ms} ms for {len(venues)} venues")
    if st_f.value:
        for r in st_f.value.readings:
            kind = "regulatory" if r.regulatory else "low-cost"
            print(f"station {r.source}:{r.uid} {r.name[:40]} — {r.provider} [{kind}] "
                  f"PM2.5 {r.pm25_ugm3} µg/m³ @ {r.observed_at.astimezone(IST):%d %b %H:%M} IST")
        for s in st_f.value.stale:
            print(f"stale  {s.name[:40]} — {s.provider} ({s.distance_km} km, last {s.last_seen.astimezone(IST):%d %b %H:%M} IST)")

    for v, sources, micro, hours, exposure, bias, aq in results[:TOP_VENUES]:
        print(f"\n== {v.name} ({v.kind}, {v.distance_km:.1f} km) @ {v.lat:.5f},{v.lon:.5f}")
        print(f"   green {micro.green_share:.0%} · water {micro.water_share:.0%} · nearest water "
              + (f"{micro.water_distance_m:.0f} m" if micro.water_distance_m is not None else "> 500 m")
              + (" (near water)" if micro.near_water else ""))
        if bias:
            print(f"   station bias0 {bias.bias0:.2f} (stations {bias.station_pm25} vs CAMS {bias.cams_pm25} "
                  f"at {bias.t_obs:%H:%M} IST{', low-cost used' if bias.low_cost else ''})")
        else:
            print("   station bias: none (CAMS as is)")
        print("   sources (top 8 by w·exp(-d/400)):")
        for s in sources:
            print(f"     w={s.weight:.2f}  {describe(s)[:62]:62} bearing {s.bearing_deg:5.1f}°  "
                  f"nearest pt {s.lat:.5f},{s.lon:.5f}")
        print("   hour       wind from     km/h   E     PM2.5 bg → local [band]      NAQI          top source")
        for h, ex, a in zip(hours, exposure, aq, strict=True):
            wind = f"{h.wind_dir_deg:3.0f}° {compass_word(h.wind_dir_deg):3}" if h.wind_dir_deg is not None else "   -   "
            top = sources[ex.top[0].source] if ex.top else None
            top_txt = f"{(top.name or top.cls)[:26]} ({compass_word(top.bearing_deg)})" if top else "-"
            pm = (f"{a.pm25_bg:5.1f} → {a.pm25_local:5.1f} [{a.pm25_low:4.0f}–{a.pm25_high:4.0f}]"
                  if a.pm25_local is not None else "   unknown (beyond CAMS)   ")
            print(f"   {h.t:%a %H:%M}  {wind}  {h.wind_kmh or 0:5.1f}  {ex.exposure:4.2f}  {pm}  "
                  f"{(a.naqi.value if a.naqi else '-'):12}  {top_txt}")
        known = [(a, ex) for a, ex in zip(aq, exposure, strict=True) if a.pm25_local is not None]
        if known:
            best = min(known, key=lambda t: t[0].pm25_local)  # type: ignore[arg-type, return-value]
            worst = max(known, key=lambda t: t[0].pm25_local)  # type: ignore[arg-type, return-value]
            wt = sources[worst[1].top[0].source] if worst[1].top else None
            why = f", driven by {(wt.name or wt.cls)} upwind ({compass_word(wt.bearing_deg)})" if wt else ""
            print(f"   → Best hour {best[0].t:%a %H:%M} (PM2.5 ≈ {best[0].pm25_local:.0f} µg/m³, E {best[1].exposure:.2f}); "
                  f"worst {worst[0].t:%a %H:%M} (≈ {worst[0].pm25_local:.0f} µg/m³, E {worst[1].exposure:.2f}{why}).")
        else:
            lo = min(exposure, key=lambda e: e.exposure)
            hi = max(exposure, key=lambda e: e.exposure)
            print(f"   → AQ unknown; lowest exposure {lo.t:%a %H:%M} (E {lo.exposure:.2f}), highest {hi.t:%a %H:%M} (E {hi.exposure:.2f}).")

    print("\nassumptions:")
    for note in air_quality_assumptions(st_f.value, [r[5] for r in results], now):
        print(f"  - {note}")
    print(f"evidence records: {len(store)}")
    print(f"degraded sources: {len(degraded)}")
    for d in degraded:
        print(f"  - {d.source.value}/{d.stage} [{d.reason}] {d.detail[:120]}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit('usage: smoke_downscale.py "<place>" <activity>')

    async def run() -> None:
        try:
            await main(sys.argv[1], Activity(sys.argv[2]))
        finally:
            await close_client()

    asyncio.run(run())
