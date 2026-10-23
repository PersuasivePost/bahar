"""Step 7 smoke: five questions through a running /ask (English, Hinglish, Hindi, missing
location, out of scope); prints status, answer_text, grounding and latency for each.

    uv run uvicorn bahar.api.main:app --port 7860   # in another terminal
    uv run python scripts/smoke_ask.py [base_url]
"""

import sys
import time

import httpx

QUERIES = (
    ("English", "tennis after work today near Hinjewadi, Pune"),
    ("Hinglish", "kal subah Powai ke paas walk karni hai, mujhe asthma hai"),
    ("Hindi", "कल सुबह इंडिया गेट के पास दौड़ना है, मेरी उम्र 65 साल है"),
    ("missing location", "can I go cycling tomorrow evening?"),
    ("out of scope", "where can I go swimming in Andheri tonight?"),
)


def main(base: str) -> None:
    with httpx.Client(base_url=base, timeout=120) as client:
        for title, q in QUERIES:
            t0 = time.perf_counter()
            r = client.post("/ask", json={"query": q})
            wall = time.perf_counter() - t0
            print(f"\n{'=' * 92}\n[{title}] {q}\nHTTP {r.status_code} in {wall:.1f} s")
            if r.status_code != 200:
                print(r.text[:500])
                continue
            body = r.json()
            print(f"status: {body['status']}\n\n{body['answer_text']}\n")
            print(f"grounding: {body['grounding']}")
            print(f"latency_ms: {body['latency_ms']}")
            print(f"evidence: {len(body['evidence'])} records · assumptions: {len(body['assumptions'])} "
                  f"· degraded: {[d['source'] + '/' + d['reason'] for d in body['degraded_sources']]}")  # fmt: skip


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:7860")
