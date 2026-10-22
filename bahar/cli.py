"""Run parse -> gate -> fetch + engine (no composer yet) and pretty-print the recommendation.

    uv run python -m bahar.cli --q "kal shaam Koregaon Park mein daudna hai"
    uv run python -m bahar.cli --parsed '{"in_scope": true, "activity": "running",
        "location_text": "Koregaon Park, Pune", "time": {"day": "tomorrow", "part": "evening"},
        "lang": "en"}'

--parsed skips the language model entirely.
"""

import argparse
import asyncio
import time
from collections.abc import Iterable
from datetime import datetime

from bahar.config.activities import ACTIVITIES
from bahar.config.health import resolve_health
from bahar.config.settings import get_settings
from bahar.config.sources import IST
from bahar.config.weights import IMPROVE_SEARCH_H
from bahar.data.http import close_client
from bahar.engine.recommend import rank
from bahar.engine.run import run_engine
from bahar.llm.parser import parse_query, register_parser
from bahar.llm.runtime import LlmRuntime
from bahar.models import EngineInput, EngineResult, Factor, ParsedQuery, ScoredSlot
from bahar.pipeline import gate, gather_inputs

_W = 92


def _rule(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, _W - len(title) - 4))


def _ids(ids: Iterable[str]) -> str:
    ids = list(ids)
    return f"[{' '.join(ids)}]" if ids else ""


def _factor(f: Factor) -> str:
    delta = "  GATE" if f.gate else f"{f.delta:+6.1f}"
    return f"    {delta}  {f.code.value:18} {f.label}  {_ids(f.evidence_ids)}"


def _slot_line(s: ScoredSlot, names: dict[str, str], refs: dict[str, str]) -> str:
    return (f"{names[s.venue_id]} [{refs[s.venue_id]}] · {s.start:%a %d %b %H:%M}–{s.end:%H:%M} "
            f"· score {s.score:.1f}{' · BLOCKED' if s.blocked else ''}")  # fmt: skip


def _slot(title: str, s: ScoredSlot | None, names: dict[str, str], refs: dict[str, str]) -> None:
    if s is None:
        print(f"{title:10} —")
        return
    print(f"{title:10} {_slot_line(s, names, refs)}")
    pm = f"{s.pm25_mean:.0f} µg/m³" if s.pm25_mean is not None else "unknown"
    dose = f"{s.dose_pct:.0f}% of a resting day" if s.dose_pct is not None else "—"
    print(f"{'':10} PM2.5 {pm} · WBGT max {s.wbgt_max:.1f} °C · rain ≤ {s.rain_prob_max:.0f}% "
          f"· exposure {s.exposure_mean:.2f} · dose {dose}")  # fmt: skip
    for f in s.gates:
        print(_factor(f))
    for f in sorted(s.factors, key=lambda f: f.delta):
        print(_factor(f))


def _print(inp: EngineInput, res: EngineResult, latency: dict[str, int], parsed: ParsedQuery) -> None:
    names = {v.id: v.name for v in inp.venues}
    refs = res.venue_refs
    act = ACTIVITIES[inp.activity]
    health, _ = resolve_health(parsed.profile)
    high = act.intensity == "high"
    print(f"query      {inp.activity.value} near {inp.place.name[:70]}")
    print(f"           ({inp.place.lat:.4f}, {inp.place.lon:.4f}) · radius {inp.radius_km:g} km · "
          f"{len(inp.venues)} venues · {act.duration_min} min, {act.intensity} intensity")  # fmt: skip
    print(f"window     {inp.window.label} · {len(inp.window.starts)} start hours")
    print(f"profile    {health.key} · PM2.5 limit {health.pm25_limit_high if high else health.pm25_limit:g} µg/m³ "
          f"· WBGT stop {health.wbgt_stop_high if high else health.wbgt_stop:g} °C · UV ≥ {health.uv_threshold:g}")  # fmt: skip
    print("timings    " + " · ".join(f"{k} {v} ms" for k, v in latency.items()))

    rec = res.recommendation
    if rec is None:
        print("\nno recommendation:", "; ".join(res.assumptions))
        return
    _rule(f"{rec.status.upper()} · confidence {rec.confidence:.2f}")
    if rec.improves_at:
        print(f"improves   from {rec.improves_at:%a %d %b %H:%M} (first safe start after the window)")
    elif rec.status == "no_safe_window":
        print(f"improves   not within {IMPROVE_SEARCH_H} h after the window")
    _slot("BEST", rec.best, names, refs)
    _slot("ALT VENUE", rec.alt_venue, names, refs)
    _slot("ALT TIME", rec.alt_time, names, refs)
    if rec.why_not:
        wn = rec.why_not
        print(f"WHY NOT    {_slot_line(wn.slot, names, refs)}")
        print(f"{'':10} decisive: {wn.decisive.code.value} — {wn.decisive.label} "
              f"({'gate' if wn.decisive.gate else f'{wn.decisive.delta:+.1f}'}) {_ids(wn.decisive.evidence_ids)}")  # fmt: skip

    _rule("ADVICE")
    adv = res.advice
    if adv:
        for title, items in (("carry", adv.carry), ("watch for", adv.watch_for)):
            print(f"{title:10} " + ("—" if not items else ""))
            for it in items:
                print(f"    - {it.label}  {_ids(it.evidence_ids)}")
        print(f"{'avoid':10} " + ("—" if not adv.avoid_when else ""))
        for r in adv.avoid_when:
            print(f"    - {r.start:%a %H:%M}–{r.end:%H:%M}  {r.reason.value}  {_ids(r.evidence_ids[:4])}")

    _rule("LEADERBOARD (best slot per venue)")
    best_per: dict[str, ScoredSlot] = {}
    for s in rank(res.slots, {v.id: v.distance_km for v in inp.venues}):
        best_per.setdefault(s.venue_id, s)
    print(f"  {'venue':34} {'km':>5} {'start':>9} {'score':>6} {'PM2.5':>6} {'WBGT':>5} {'E':>5}  gates / top penalty")
    for vid, s in best_per.items():
        v = next(v for v in inp.venues if v.id == vid)
        worst = min(s.factors, key=lambda f: f.delta, default=None)
        why = ", ".join(g.code.value for g in s.gates) or (f"{worst.code.value} {worst.delta:+.0f}" if worst else "")
        pm = f"{s.pm25_mean:6.0f}" if s.pm25_mean is not None else f"{'?':>6}"
        print(f"  {v.name[:34]:34} {v.distance_km:5.1f} {s.start:%a %H:%M} {s.score:6.1f} {pm} "
              f"{s.wbgt_max:5.1f} {s.exposure_mean:5.2f}  {why}")  # fmt: skip

    _rule("ASSUMPTIONS / DEGRADED / NOTES")
    for a in res.assumptions:
        print(f"  - {a}")
    for d in inp.degraded:
        print(f"  ! {d.source.value}/{d.stage} [{d.reason}] {d.detail[:110]}")
    for n in res.notes:
        print(f"  * {n}")

    cited: list[str] = [*refs.values()]
    for s in filter(None, (rec.best, rec.alt_venue, rec.alt_time, rec.why_not.slot if rec.why_not else None)):
        cited += [i for f in (*s.gates, *s.factors) for i in f.evidence_ids]
    if rec.why_not:
        cited += rec.why_not.decisive.evidence_ids
    if adv:
        cited += [i for it in (*adv.carry, *adv.watch_for) for i in it.evidence_ids]
        cited += [i for r in adv.avoid_when for i in r.evidence_ids]
    by_id = {e.id: e for e in res.evidence}
    cited = [i for i in dict.fromkeys(cited) if i in by_id]
    _rule(f"CITED EVIDENCE ({len(cited)} of {len(res.evidence)} records)")
    for i in cited[:60]:
        e = by_id[i]
        unit = f" {e.unit}" if e.unit else ""
        note = f" — {e.note}" if e.note else ""
        print(f"  {i:5} {e.label}: {e.value}{unit} ({e.source.value}){note[:90]}")
    if len(cited) > 60:
        print(f"  … {len(cited) - 60} more")


async def _main(parsed: ParsedQuery, now: datetime) -> None:
    try:
        t0 = time.perf_counter()
        g = await gather_inputs(parsed, now)
        latency = {**g.latency_ms, "fetch_total": int((time.perf_counter() - t0) * 1000)}
        if g.engine_input is None:
            print(f"status {g.status}: {g.detail}")
            for d in g.degraded:
                print(f"  ! {d.source.value}/{d.stage} [{d.reason}] {d.detail[:110]}")
            return
        t0 = time.perf_counter()
        res = run_engine(g.engine_input)
        latency["engine"] = int((time.perf_counter() - t0) * 1000)
        _print(g.engine_input, res, latency, parsed)
    finally:
        await close_client()


def _parse(query: str, now: datetime) -> ParsedQuery | None:
    """Load the model, register the parser prefix, parse; print the parse and the gate.
    Returns the ParsedQuery when it passes the gate."""
    runtime = LlmRuntime(get_settings())
    runtime.load()
    h = runtime.health
    if h.model_loaded:
        t0 = time.perf_counter()
        n = register_parser(runtime)
        print(f"model      {h.model_file} · {h.n_threads} threads · parser prefix {n} tokens "
              f"in {int((time.perf_counter() - t0) * 1000)} ms")  # fmt: skip
    else:
        print(f"model      NOT LOADED ({h.error}) — rule-based fallback")
    res = asyncio.run(parse_query(runtime, query, now))
    _rule(f"PARSED · {res.ms} ms{' · FALLBACK' if res.fallback else ''}")
    print(f"raw        {res.raw or '—'}")
    print(res.parsed.model_dump_json(by_alias=True, exclude_defaults=True, indent=2))
    for a in res.assumptions:
        print(f"  - {a}")
    stop = gate(res.parsed)
    _rule(f"GATE · {stop.status.value if stop else 'pass'}")
    if stop:
        print(f"{stop.reason}: {stop.message}")
        return None
    return res.parsed


def main() -> None:
    ap = argparse.ArgumentParser(description="Bahar parse + fetch + engine (no composer)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--q", help="free-text question (runs the SLM parser)")
    src.add_argument("--parsed", help="ParsedQuery JSON (skips the parser)")
    ap.add_argument("--now", help="ISO datetime (IST if naive); default: the current time")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    parsed = _parse(args.q, now) if args.q else ParsedQuery.model_validate_json(args.parsed)
    if parsed is not None:
        _rule("RECOMMENDATION")
        asyncio.run(_main(parsed, now))


if __name__ == "__main__":
    main()
