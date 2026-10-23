"""Query battery: runs eval/queries.jsonl against a live Bahar and prints a failure report.

    uv run python scripts/battery.py [base_url]          (default http://127.0.0.1:7860)

Direct loopback calls are exempt from the /ask rate limit, so they run 2 at a time; any other
URL (the Cloudflare tunnel) runs sequentially with a 7 s gap to stay under 10 req/min.
Venue city is the best venue's Nominatim reverse geocode (1 req/s), read from the map page."""

import asyncio
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # no installed package

from bahar.config.settings import get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SLOW_S = 30.0
GAP_S = 7.0
UNFILLED = re.compile(r"\{\{\s*[ev]\d+\s*\}\}")
MAP_DATA = re.compile(r"const S = (\{.*?\});\n", re.S)
# Spellings of one city across OSM / Nominatim and users.
ALIASES = {"bengaluru": "bangalore", "gurugram": "gurgaon", "mysuru": "mysore", "kochi": "cochin",
           "panaji": "panjim", "thiruvananthapuram": "trivandrum"}  # fmt: skip
CAUSE_ORDER = ["error", "geocode", "parse", "no_venues", "grounding_fallback", "latency"]


class Reverse:
    """Nominatim reverse geocoder, <= 1 req/s, memoised."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client, self.last, self.cache = client, 0.0, {}
        email = get_settings().nominatim_email
        self.headers = {"User-Agent": f"Bahar-battery/0.1 ({email})"}

    async def address(self, lat: float, lon: float) -> list[str]:
        key = (round(lat, 3), round(lon, 3))
        if key not in self.cache:
            await asyncio.sleep(max(0.0, self.last + 1.1 - time.monotonic()))
            self.last = time.monotonic()
            try:
                r = await self.client.get(
                    "https://nominatim.openstreetmap.org/reverse", headers=self.headers, timeout=15,
                    params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "addressdetails": 1})
                self.cache[key] = [str(v).lower() for v in r.json().get("address", {}).values()]
            except Exception:
                self.cache[key] = []
        return self.cache[key]


def city_matches(expected: str, address: list[str]) -> bool:
    names = {expected.lower(), ALIASES.get(expected.lower(), expected.lower())}
    return any(n in a or a in n for n in names for a in address if a)


async def run_one(api: httpx.AsyncClient, rev: Reverse, base: str, q: dict) -> dict:
    out = {**q, "http": None, "status": None}
    t0 = time.perf_counter()
    try:
        r = await api.get(f"{base}/ask", params={"q": q["query"]}, timeout=90)
        out["http"] = r.status_code
        body = r.json()
    except Exception as e:
        out["wall_s"] = round(time.perf_counter() - t0, 1)
        out["exception"] = f"{type(e).__name__}: {e}"
        return out
    out["wall_s"] = round(time.perf_counter() - t0, 1)
    if r.status_code != 200:
        out["exception"] = f"HTTP {r.status_code}"
        return out
    g = body["grounding"]
    parsed = body.get("parsed") or {}
    out |= {
        "status": body["status"], "answer_text": body["answer_text"],
        "latency_ms": body["latency_ms"], "composer_mode": g["mode"],
        "validator_passed": g["validator_passed"], "fallback_used": g["fallback_used"],
        "retries": g["retries"], "rejections": g["rejections"],
        "degraded": sorted({d["source"] for d in body["degraded_sources"]}),
        "assumptions": body["assumptions"], "lang": parsed.get("lang"),
        "parsed_activity": parsed.get("activity"), "parsed_location": parsed.get("location_text"),
        "unfilled_refs": UNFILLED.findall(body["answer_text"]), "map_url": body.get("map_url"),
    }
    if body.get("map_url"):
        mp = await api.get(f"{base}/map/{body['query_id']}")
        if (m := MAP_DATA.search(mp.text)) is not None:
            snap = json.loads(m.group(1))
            best = next(v for v in snap["venues"] if v["id"] == snap["best_venue_id"])
            out["venue"] = best["name"]
            out["venue_city_address"] = await rev.address(best["lat"], best["lon"])
            if q["expected_city"]:
                out["city_ok"] = city_matches(q["expected_city"], out["venue_city_address"])
    return out


def causes(r: dict) -> list[str]:
    """Why a result counts as a failure (empty = fine)."""
    out = []
    if r["status"] is None or r["status"] == "error":
        out.append("error")
    exp, st = r["expect"], r["status"]
    if exp == "answer" and st == "needs_clarification":
        out.append("geocode" if r.get("parsed_location") else "parse")
    elif exp == "answer" and st == "out_of_scope":
        out.append("parse")
    elif exp != "answer" and st not in (None, "error") and st != exp:
        out.append("parse")
    if r.get("city_ok") is False:
        out.append("geocode")
    if st == "no_venues":
        out.append("no_venues")
    if r.get("fallback_used") or r.get("composer_mode") == "template":
        out.append("grounding_fallback")
    if r.get("unfilled_refs"):
        out.append("grounding_fallback")
    if (r.get("wall_s") or 0) > SLOW_S:
        out.append("latency")
    return list(dict.fromkeys(out))


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")


def report(results: list[dict]) -> None:
    n = len(results)
    ok = [r for r in results if r["status"]]
    walls = [r["wall_s"] for r in results if r.get("wall_s") is not None]
    print(f"\n=== {n} queries ===")
    print("status:", dict(Counter(r["status"] or "no-response" for r in results)))
    print(f"latency s: p50 {statistics.median(walls):.1f}  p95 {pct(walls, .95):.1f}  max {max(walls):.1f}"
          f"  (>{SLOW_S:g}s: {sum(w > SLOW_S for w in walls)})")
    answered = [r for r in ok if r["composer_mode"] != "none"]
    if answered:
        print(f"validator pass {sum(r['validator_passed'] for r in answered)}/{len(answered)}"
              f" · fallback {sum(r['fallback_used'] for r in answered)}/{len(answered)}"
              f" · retries {sum(r['retries'] for r in answered)}")
        print("composer mode:", dict(Counter(r["composer_mode"] for r in answered)))
    print("unfilled refs:", sum(bool(r.get("unfilled_refs")) for r in ok), "answers")
    checked = [r for r in ok if "city_ok" in r]
    print(f"venue city = expected: {sum(r['city_ok'] for r in checked)}/{len(checked)}")
    print("degraded sources:", dict(Counter(s for r in ok for s in r["degraded"])))
    by = defaultdict(list)
    for r in results:
        for c in causes(r):
            by[c].append(r)
    print("\n=== failures by cause ===")
    ranked = []
    for c in CAUSE_ORDER:
        if c in by:
            print(f"{c}: {len(by[c])}")
            ranked += [(c, r) for r in by[c]]
    print("\n=== top 10 ===")
    for c, r in ranked[:10]:
        why = r.get("exception") or (r["rejections"][-1][:90] if c == "grounding_fallback" and r["rejections"]
              else f"status={r['status']} expected_city={r['expected_city']} venue={r.get('venue')}"
                   f" wall={r['wall_s']}s")  # fmt: skip
        print(f"[{c}] #{r['id']} {r['query'][:70]!r}\n      {why}")


async def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7860").rstrip("/")
    local = urlparse(base).hostname in ("127.0.0.1", "localhost", "::1")
    queries = [json.loads(line) for line in (ROOT / "eval/queries.jsonl").read_text().splitlines() if line]
    out_dir = ROOT / "eval/results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    results: list[dict] = []
    async with httpx.AsyncClient() as client:
        rev = Reverse(client)
        sem = asyncio.Semaphore(2 if local else 1)

        async def task(i: int, q: dict) -> None:
            async with sem:
                if not local and i:
                    await asyncio.sleep(GAP_S)
                r = await run_one(client, rev, base, q)
                results.append(r)
                with path.open("a") as f:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                print(f"{len(results):>2}/{len(queries)} #{q['id']} {r['status']} {r.get('wall_s')}s", flush=True)

        await asyncio.gather(*(task(i, q) for i, q in enumerate(queries)))
    results.sort(key=lambda r: r["id"])
    report(results)
    print(f"\nsaved {path}")


if __name__ == "__main__":
    asyncio.run(main())
