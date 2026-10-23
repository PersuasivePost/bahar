"""Self-check of the local OSM index for Powai / running: feature counts and query times,
local vs the Overpass result (the cache entry if there is one, else a live query).

    uv run python scripts/check_osm_index.py"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # no installed package

from bahar.config.activities import ACTIVITIES  # noqa: E402
from bahar.data import cache, localosm  # noqa: E402
from bahar.data import overpass as ov  # noqa: E402
from bahar.data.geocode import geocode, search_radius_km  # noqa: E402
from bahar.data.http import close_client  # noqa: E402
from bahar.models import Activity  # noqa: E402

PLACE, ACTIVITY = "Powai, Mumbai", Activity.running


def timed(fn, *args):
    t0 = time.perf_counter()
    out = fn(*args)
    return out, time.perf_counter() - t0


async def main() -> None:
    print(f"index: {localosm.source_label()}")
    place = (await geocode(PLACE)).value
    if place is None:
        sys.exit("geocode failed")
    radius = search_radius_km(place, ACTIVITIES[ACTIVITY].radius_km)
    print(f"{PLACE} -> {place.lat:.4f}, {place.lon:.4f}, radius {radius:g} km")

    local_v, t_v = timed(localosm.fetch_venues, ACTIVITY, place.lat, place.lon, radius)
    if local_v is None or local_v.value is None:
        sys.exit("local index does not cover this area")
    print(f"local venues:   {len(local_v.value):>3} in {t_v:.2f} s (target < 0.5 s)")

    key = cache.make_key("venues", activity=ACTIVITY.value, lat=round(place.lat, 3), lon=round(place.lon, 3), r=radius)
    entry = cache._cache().get(key)
    live_v = entry[1] if entry else await ov._fetch_venues(ACTIVITY, place.lat, place.lon, radius)
    over_v = live_v.value
    print(f"overpass venues: {len(over_v) if over_v is not None else 'n/a':>3} ({'cached' if entry else 'live'})"
          + ("" if over_v else f" {[d.detail for d in live_v.degraded]}"))
    if over_v:
        a, b = {v.name for v in local_v.value}, {v.name for v in over_v}
        print(f"  only local: {sorted(a - b)}\n  only overpass: {sorted(b - a)}")

    venues = over_v or local_v.value
    local_c, t_c = timed(localosm.fetch_context, venues)
    n = len(venues)
    print(f"local context:  {len(local_c.value.features):>3} features for {n} venues in {t_c:.2f} s (target < 2 s)")
    ckey = cache.make_key("context", ids=sorted(v.id for v in venues))
    centry = cache._cache().get(ckey)
    live_c = centry[1] if centry else await ov._fetch_context(venues)
    if live_c.value is not None:
        by = lambda fs: {g: sum(f.group == g for f in fs) for g in ("road", "industry", "power", "green", "water")}  # noqa: E731
        print(f"overpass context: {len(live_c.value.features):>3} ({'cached' if centry else 'live'})")
        print(f"  local    {by(local_c.value.features)}\n  overpass {by(live_c.value.features)}")
    else:
        print("overpass context: n/a", [d.detail for d in live_c.degraded])
    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
