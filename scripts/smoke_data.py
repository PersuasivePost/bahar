"""Run the whole fetch chain for a place + activity and print a compact summary.

    uv run python scripts/smoke_data.py "Powai, Mumbai" running
"""

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # no installed package

from bahar.config.activities import ACTIVITIES  # noqa: E402
from bahar.config.sources import CONTEXT_RADIUS_M, IST, MICROCLIMATE_RADIUS_M  # noqa: E402
from bahar.data.airquality import fetch_air_quality  # noqa: E402
from bahar.data.elevation import fetch_elevation  # noqa: E402
from bahar.data.geocode import geocode, search_radius_km  # noqa: E402
from bahar.data.http import close_client  # noqa: E402
from bahar.data.overpass import fetch_context, fetch_venues  # noqa: E402
from bahar.data.result import Fetch  # noqa: E402
from bahar.data.routing import fetch_travel  # noqa: E402
from bahar.data.stations import fetch_stations  # noqa: E402
from bahar.data.weather import fetch_forecast  # noqa: E402
from bahar.engine.geo import haversine_km  # noqa: E402
from bahar.models import Activity, DegradedSource, Evidence, Venue  # noqa: E402

degraded_all: list[DegradedSource] = []
evidence_all: list[Evidence] = []
timings: list[tuple[str, Fetch]] = []  # type: ignore[type-arg]


def take(stage: str, fetch: Fetch):  # type: ignore[type-arg]
    timings.append((stage, fetch))
    degraded_all.extend(fetch.degraded)
    evidence_all.extend(fetch.evidence)
    return fetch.value


def feature_counts(venue: Venue, features: list) -> dict[str, int]:  # type: ignore[type-arg]
    """Features with any vertex inside their group's radius (2 km sources, 500 m green/water)."""
    counts: dict[str, int] = {}
    for f in features:
        limit = MICROCLIMATE_RADIUS_M if f.group in ("green", "water") else CONTEXT_RADIUS_M
        near = any(
            haversine_km(venue.lat, venue.lon, la, lo) * 1000 <= limit
            for ring in f.rings
            for la, lo in ring
        )
        if near:
            counts[f.group] = counts.get(f.group, 0) + 1
    return counts


async def main(place_text: str, activity: Activity) -> None:
    place = take("geocode", await geocode(place_text))
    if place is None:
        print("geocode failed:", degraded_all)
        return
    radius = search_radius_km(place, ACTIVITIES[activity].radius_km)
    print(f"place   {place.name}\n        ({place.lat:.4f}, {place.lon:.4f}) rank={place.place_rank} "
          f"city={place.is_city} -> radius {radius:g} km")  # fmt: skip

    venues = take("venues", await fetch_venues(activity, place.lat, place.lon, radius))
    if not venues:
        print("no venues;", degraded_all)
        return

    started = time.perf_counter()
    results = await asyncio.gather(
        fetch_context(venues),
        fetch_forecast(venues),
        fetch_air_quality(venues),
        fetch_stations(venues),
        fetch_travel((place.lat, place.lon), venues),
    )
    parallel_ms = int((time.perf_counter() - started) * 1000)
    context, forecast, aq, stations, travel = (
        take(name, r)
        for name, r in zip(["context", "forecast", "airquality", "stations", "routing"], results)
    )
    elevations = None
    if activity is Activity.hiking:
        elevations = take("elevation", await fetch_elevation([(v.lat, v.lon) for v in venues]))

    print(f"\n{len(venues)} venues")
    travel_by_id = {t.venue_id: t for t in travel or []}
    for v in venues:
        t = travel_by_id.get(v.id)
        area = f"{v.area_km2:.2f} km2" if v.area_km2 is not None else "point"
        ctx = feature_counts(v, context.features) if context else {}
        print(f"  {v.name[:34]:34} {v.kind[:14]:14} {v.distance_km:5.1f} km {area:>10}"
              f"  {t.minutes:4.0f} min{'~' if t and t.estimated else ' '}"
              f"  ctx={ctx or '-'}")  # fmt: skip

    if forecast:
        vw = next(iter(forecast.venues.values()))
        now = datetime.now(IST).replace(minute=0, second=0, microsecond=0)
        idx = next((i for i, h in enumerate(vw.hours) if h.t >= now), 0)
        print(f"\nforecast: {len(vw.hours)} hourly rows x {len(forecast.venues)} venues; "
              f"sunrise/sunset days: {len(vw.sunrise)}")  # fmt: skip
        aq_hours = aq.venues[vw.venue_id].hours if aq else []
        print("  hour    T°C  RH  rain% mm  wind(gust)@dir  UV  CAPE  PM2.5 PM10")
        for h in vw.hours[idx : idx + 24 : 4]:
            a = next((x for x in aq_hours if x.t == h.t), None)
            print(f"  {h.t:%d %H:%M} {h.temp_c:4.1f} {h.rh:3.0f} {h.rain_prob or 0:5.0f} {h.rain_mm or 0:4.1f}"
                  f"  {h.wind_kmh:4.1f}({h.gust_kmh:4.1f})@{h.wind_dir_deg:3.0f}  {h.uv or 0:3.1f} "
                  f"{h.cape or 0:5.0f}  {a.pm25 if a and a.pm25 is not None else float('nan'):5.1f} "
                  f"{a.pm10 if a and a.pm10 is not None else float('nan'):5.1f}")  # fmt: skip
        past_rain = sum(h.rain_mm or 0 for h in vw.hours if h.t < now)
        print(f"  past-48h rain at first venue: {past_rain:.1f} mm")

    readings = stations.readings if stations else []
    print(f"\nstations: {len(readings)}")
    for s in readings:
        print(f"  {s.source} {s.name[:44]:44} PM2.5 {s.pm25_ugm3:6.1f} µg/m³"
              + (f" (AQI {s.aqi_pm25})" if s.aqi_pm25 is not None else "")
              + f" obs {s.observed_at.astimezone(IST):%H:%M} IST")  # fmt: skip

    if elevations:
        print("\nelevation m:", [None if e is None else round(e) for e in elevations])

    print("\nper-source timings (ms; * = cache hit)")
    for stage, f in timings:
        print(f"  {stage:11} {f.elapsed_ms:6d}{'*' if f.cached else ' '}")
    print(f"  {'parallel':11} {parallel_ms:6d}  (wall time of the 5 concurrent fetches)")
    print(f"\nevidence records: {len(evidence_all)}")
    print(f"degraded sources: {len(degraded_all)}")
    for d in degraded_all:
        print(f"  - {d.source.value}/{d.stage} [{d.reason}] {d.detail[:110]}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit('usage: smoke_data.py "<place>" <activity>')

    async def run() -> None:
        try:
            await main(sys.argv[1], Activity(sys.argv[2]))
        finally:
            await close_client()

    asyncio.run(run())
