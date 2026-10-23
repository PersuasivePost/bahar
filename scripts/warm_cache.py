"""Warm the static-geography cache (7-day TTL): geocode, venues and pollution-source context for
~40 places x 11 activities, through the same functions /ask uses (gather_inputs), so the cache
keys match. Weather and air quality are live (15 min TTL) and are deliberately not warmed.

    uv run python scripts/warm_cache.py

Sequential, 3 s between combinations, resumable (done combinations are kept in
CACHE_DIR/warm_done.json), failures appended to CACHE_DIR/warm_failures.log."""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # no installed package

from bahar.config.activities import ACTIVITIES  # noqa: E402
from bahar.config.settings import get_settings  # noqa: E402
from bahar.data.geocode import search_radius_km  # noqa: E402
from bahar.data.http import close_client  # noqa: E402
from bahar.data.overpass import fetch_context, fetch_venues  # noqa: E402
from bahar.models import Activity, ParsedQuery  # noqa: E402
from bahar.pipeline import _geocode  # noqa: E402

GAP_S = 3.0
ROOT = Path(__file__).resolve().parent.parent
PLACES = [
    "Powai, Mumbai", "Andheri, Mumbai", "Bandra, Mumbai", "Koregaon Park, Pune", "Whitefield, Bengaluru",
    "Hinjewadi, Pune", "Lonavala", "Mumbai", "Delhi", "Bengaluru", "Chennai", "Hyderabad", "Kolkata",
    "Pune", "Ahmedabad", "Gurugram", "Noida", "Thane", "Navi Mumbai", "Jaipur", "Lucknow", "Nagpur",
]  # the cities in eval/queries.jsonl are added below


def places() -> list[str]:
    cities = {json.loads(line)["expected_city"] for line in (ROOT / "eval/queries.jsonl").read_text().splitlines() if line}
    return list(dict.fromkeys([*PLACES, *sorted(c for c in cities if c)]))


async def warm(place: str, activity: Activity) -> str | None:
    """None on success, else the reason it failed."""
    parsed = ParsedQuery(in_scope=True, activity=activity, location_text=place, lang="en")  # type: ignore[arg-type]
    geo = await _geocode(parsed, place)
    if geo.value is None:
        return "geocode: " + "; ".join(d.detail for d in geo.degraded)
    radius = search_radius_km(geo.value, ACTIVITIES[activity].radius_km)
    venues = await fetch_venues(activity, geo.value.lat, geo.value.lon, radius)
    if venues.value is None or venues.degraded:
        return "venues: " + "; ".join(d.detail for d in venues.degraded)
    if not venues.value:
        return None  # a real "nothing here": cached as such, nothing more to fetch
    ctx = await fetch_context(venues.value)
    if ctx.degraded:
        return "context: " + "; ".join(d.detail for d in ctx.degraded)
    return None


async def main() -> None:
    cache_dir = Path(get_settings().cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    done_file, fail_file = cache_dir / "warm_done.json", cache_dir / "warm_failures.log"
    done: set[str] = set(json.loads(done_file.read_text())) if done_file.exists() else set()
    combos = [(p, a) for p in places() for a in Activity]
    todo = [(p, a) for p, a in combos if f"{p}|{a.value}" not in done]
    print(f"{len(combos)} combinations, {len(todo)} to do", flush=True)
    try:
        for i, (place, activity) in enumerate(todo, 1):
            t0 = time.monotonic()
            try:
                problem = await warm(place, activity)
            except Exception as exc:  # keep going: one bad combination must not stop the run
                problem = f"{type(exc).__name__}: {exc}"
            if problem is None:
                done.add(f"{place}|{activity.value}")
                done_file.write_text(json.dumps(sorted(done)))
            else:
                with fail_file.open("a") as f:
                    f.write(f"{place} | {activity.value} | {problem[:300]}\n")
            print(f"{i}/{len(todo)} {place} / {activity.value}: {'ok' if problem is None else 'FAILED'}", flush=True)
            await asyncio.sleep(max(0.0, GAP_S - (time.monotonic() - t0)))
    finally:
        await close_client()


if __name__ == "__main__":
    asyncio.run(main())
