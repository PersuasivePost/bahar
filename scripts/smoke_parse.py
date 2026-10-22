"""Parse 32 messy queries with the SLM parser and apply the gates (no data fetching).

    uv run python scripts/smoke_parse.py

Prints query | parsed fields | gate | ms | misses and saves the table plus the raw model output
to eval/parse_smoke.md and eval/parse_smoke.jsonl. `expect` holds the fields a correct parse
must have; misses are listed, not fixed (they are fine-tuning targets).
"""

import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # no installed package

from bahar.config.settings import get_settings  # noqa: E402
from bahar.config.sources import IST  # noqa: E402
from bahar.llm.parser import parse_query, register_parser  # noqa: E402
from bahar.llm.runtime import LlmRuntime  # noqa: E402
from bahar.models import ParsedQuery, ParseResult  # noqa: E402
from bahar.pipeline import gate  # noqa: E402

PASS = "pass"
NC = "needs_clarification"
OOS = "out_of_scope"

# (query, expected) — expected keys: act, lang, gate, day, part, hour, age, cond. Places
# differ from the parser's few-shots on purpose.
QUERIES: list[tuple[str, dict[str, Any]]] = [
    ("morning run near Marine Drive tomorrow, 6ish",
     {"act": "running", "lang": "en", "gate": PASS, "day": "tomorrow"}),
    ("kal subah Aarey mein jogging karni hai, dama hai mujhe",
     {"act": "running", "lang": "hinglish", "gate": PASS, "day": "tomorrow", "cond": "asthma"}),
    ("best park to walk with my 70 yr old dad in Indiranagar this evening",
     {"act": "walking", "lang": "en", "gate": PASS}),
    ("golf tee time sunday near Gurgaon within 30 km",
     {"act": "golf", "lang": "en", "gate": PASS, "day": "weekday:sun"}),
    ("box cricket kal raat 9 baje Thane mein, 10 log",
     {"act": "cricket", "lang": "hinglish", "gate": PASS, "day": "tomorrow", "hour": 21}),
    ("फुटबॉल खेलने के लिए शनिवार शाम को साल्ट लेक के पास कोई अच्छा मैदान?",
     {"act": "football", "lang": "hi", "gate": PASS, "day": "weekday:sat"}),
    ("उद्या सकाळी कोथरूडमध्ये धावायला जायचं आहे, मला दमा आहे",
     {"act": "running", "lang": "mr", "gate": PASS, "day": "tomorrow", "part": "morning",
      "cond": "asthma"}),
    ("tenis court near banjara hils tmrw evng",
     {"act": "tennis", "lang": "en", "gate": PASS, "day": "tomorrow"}),
    ("trek plan for next weekend near Pune, 4 friends, need an easy trail",
     {"act": "hiking", "lang": "en", "gate": PASS, "day": "next_weekend"}),
    ("picnic with kids at Sanjay Gandhi National Park parso",
     {"act": "picnic", "lang": "hinglish", "gate": PASS, "day": "day_after"}),
    ("golden hour photography at Gateway of India tomorrow",
     {"act": "photography", "lang": "en", "gate": PASS, "day": "tomorrow"}),
    ("sunrise ke time photos kheechne hain Marine Drive pe",
     {"act": "photography", "lang": "hinglish", "gate": PASS}),
    ("birthday party outdoors for 25 people in a garden near Aundh, any time in the next 3 days",
     {"act": "event", "lang": "en", "gate": PASS, "day": "next_n_days"}),
    ("I'm 7 months pregnant, is an evening stroll at Deer Park Hauz Khas ok today?",
     {"act": "walking", "lang": "en", "gate": PASS, "day": "today", "cond": "pregnancy"}),
    ("cycling ride this weekend around Sukhna Lake Chandigarh",
     {"act": "cycling", "lang": "en", "gate": PASS, "day": "this_weekend"}),
    ("cycle se office jaana hai Ghatkopar se BKC, kal 8 baje",
     {"act": "cycling", "lang": "hinglish", "gate": PASS, "day": "tomorrow"}),
    ("I want to cycle to work tomorrow morning",
     {"act": "cycling", "lang": "en", "gate": NC, "day": "tomorrow"}),
    ("commute by bike to Powai tomorrow 9am",
     {"act": "cycling", "lang": "en", "gate": NC, "day": "tomorrow"}),
    ("is it ok to go running tomorrow evening? heart patient",
     {"act": "running", "lang": "en", "gate": NC, "day": "tomorrow", "cond": "heart"}),
    ("kal shaam walk karne jaana hai",
     {"act": "walking", "lang": "hinglish", "gate": NC, "day": "tomorrow"}),
    ("best biryani in Hyderabad?", {"lang": "en", "gate": OOS}),
    ("swimming kal subah kahan karu Vashi mein", {"lang": "hinglish", "gate": OOS}),
    ("मौसम कैसा रहेगा कल दिल्ली में?", {"lang": "hi", "gate": OOS}),
    ("football turf in Whitefield aaj 7 baje",
     {"act": "football", "lang": "hinglish", "gate": PASS, "day": "today"}),
    ("golf karna hai kal dopahar, Delhi Golf Club",
     {"act": "golf", "lang": "hinglish", "gate": PASS, "day": "tomorrow"}),
    ("evening walk Bandra reclamation pe 6 baje, saath mein 2 bacche",
     {"act": "walking", "lang": "hinglish", "gate": PASS}),
    ("गिरगांव चौपाटी पर रविवार को पिकनिक, साथ में बुज़ुर्ग माँ भी हैं",
     {"act": "picnic", "lang": "hi", "gate": PASS, "day": "weekday:sun", "age": "senior"}),
    ("आज संध्याकाळी पाषाण तलावाजवळ सूर्यास्ताच्या वेळी फोटोग्राफी करायची आहे",
     {"act": "photography", "lang": "mr", "gate": PASS, "day": "today", "part": "evening"}),
    ("runnin 5k tmrw morng near lalbagh blr",
     {"act": "running", "lang": "en", "gate": PASS, "day": "tomorrow"}),
    ("cricket on 1st nov at Oval Maidan, will the ground be wet?",
     {"act": "cricket", "lang": "en", "gate": PASS, "day": "date:2026-11-01"}),
    ("kal subah Powai ke paas kuch outdoor karna hai, kya best rahega?",
     {"lang": "hinglish", "gate": NC, "day": "tomorrow"}),
    ("मुझे कल जिम जाना है, कौन सा अच्छा रहेगा अंधेरी में?", {"lang": "hi", "gate": OOS}),
]  # fmt: skip


def _fields(p: ParsedQuery) -> str:
    t = p.time
    when = "/".join(filter(None, (
        t.day, f"n{t.n_days}" if t.n_days else None, t.part.value if t.part else None,
        f"h{t.hour}" if t.hour is not None else None, f"~{t.around}" if t.around else None,
    )))  # fmt: skip
    who = ",".join(filter(None, (
        p.profile.age_group, *p.profile.conditions,
        f"x{p.profile.group_size}" if p.profile.group_size else None,
    )))  # fmt: skip
    extra = []
    if p.radius_km:
        extra.append(f"r{p.radius_km:g}km")
    if p.commute:
        extra.append(f"{p.commute.from_ or '?'}→{p.commute.to or '?'}")
    if p.preferences:
        extra.append("+" + ";".join(p.preferences))
    parts = [p.activity.value if p.activity else "—", f"@{p.location_text or '—'}", when or "—"]
    return " · ".join([*parts, *filter(None, (who, " ".join(extra)))]) + f" · {p.lang.value}"


def _misses(res: ParseResult, gate_name: str, exp: dict[str, Any]) -> list[str]:
    p = res.parsed
    got = {
        "act": p.activity.value if p.activity else None,
        "lang": p.lang.value,
        "gate": gate_name,
        "day": p.time.day,
        "part": p.time.part.value if p.time.part else None,
        "hour": p.time.hour,
        "age": p.profile.age_group,
    }
    out = [f"{k}={got[k]}≠{v}" for k, v in exp.items() if k in got and got[k] != v]
    if "cond" in exp and exp["cond"] not in p.profile.conditions:
        out.append(f"cond∌{exp['cond']}")
    return out


async def _run(runtime: LlmRuntime, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for q, exp in QUERIES:
        res = await parse_query(runtime, q, now)
        stop = gate(res.parsed)
        gate_name = stop.status.value if stop else PASS
        rows.append({
            "query": q, "parsed": _fields(res.parsed), "gate": gate_name, "ms": res.ms,
            "misses": _misses(res, gate_name, exp), "fallback": res.fallback, "raw": res.raw,
            "assumptions": res.assumptions,
            "parsed_json": res.parsed.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        })  # fmt: skip
        print(f"{len(rows):2} {res.ms:5} ms  {gate_name:20} {_fields(res.parsed)}", flush=True)
    return rows


def main() -> None:
    now = datetime.now(IST)
    runtime = LlmRuntime(get_settings())
    runtime.load()
    h = runtime.health
    if not h.model_loaded:
        sys.exit(f"model not loaded: {h.error}")
    t0 = time.perf_counter()
    n_prefix = register_parser(runtime)
    prefix_ms = int((time.perf_counter() - t0) * 1000)
    header = (
        f"{h.model_file} · {h.n_threads} threads · prompt eval {h.prompt_eval_tok_s} tok/s · "
        f"generation {h.generation_tok_s} tok/s · parser prefix {n_prefix} tokens "
        f"registered in {prefix_ms} ms · now {now:%Y-%m-%d %H:%M} IST"
    )
    print(header)
    rows = asyncio.run(_run(runtime, now))

    ms = [r["ms"] for r in rows]
    n_fields = sum(len(e) for _, e in QUERIES)
    n_miss = sum(len(r["misses"]) for r in rows)
    clean = sum(not r["misses"] for r in rows)
    fallbacks = sum(r["fallback"] for r in rows)
    summary = (
        f"{len(rows)} queries · valid JSON {len(rows) - fallbacks}/{len(rows)} · "
        f"fully correct {clean}/{len(rows)} · checked fields {n_fields - n_miss}/{n_fields} · "
        f"parse ms p50 {statistics.median(ms):.0f}, max {max(ms)}"
    )
    lines = [
        "# Parser smoke run", "", header, "", summary, "",
        "| # | query | parsed (activity · @location · time · profile · extra · lang) | gate | ms | misses |",
        "|---|---|---|---|---|---|",
    ]  # fmt: skip
    for i, r in enumerate(rows, 1):
        cells = [str(i), r["query"], r["parsed"], r["gate"], str(r["ms"]),
                 ", ".join(r["misses"]) or "✓"]  # fmt: skip
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    table = "\n".join(lines) + "\n"
    print("\n" + table)

    out = ROOT / "eval"
    out.mkdir(exist_ok=True)
    (out / "parse_smoke.md").write_text(table, encoding="utf-8")
    with (out / "parse_smoke.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"saved {out / 'parse_smoke.md'} and {out / 'parse_smoke.jsonl'}")


if __name__ == "__main__":
    main()
