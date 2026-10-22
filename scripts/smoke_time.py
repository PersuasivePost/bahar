"""Print resolved time windows for a battery of TimeSpecs at two fixed `now` values.

    uv run python scripts/smoke_time.py
"""

import sys
from datetime import date, datetime, timedelta
from itertools import groupby
from pathlib import Path

from astral import LocationInfo
from astral.sun import sun

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # no installed package

from bahar.config.sources import IST  # noqa: E402
from bahar.engine.timewindow import resolve_window  # noqa: E402
from bahar.models import Part, TimeSpec  # noqa: E402

NOWS = (datetime(2026, 10, 21, 22, 30, tzinfo=IST), datetime(2026, 10, 24, 9, 0, tzinfo=IST))
POWAI = LocationInfo("Powai", "India", "Asia/Kolkata", 19.1176, 72.9060)

SPECS: list[tuple[str, TimeSpec]] = [
    ("(nothing)", TimeSpec()),
    ("today", TimeSpec(day="today")),
    ("tomorrow", TimeSpec(day="tomorrow")),
    ("day_after", TimeSpec(day="day_after")),
    ("tomorrow morning", TimeSpec(day="tomorrow", part=Part.morning)),
    ("today evening", TimeSpec(day="today", part=Part.evening)),
    ("early_morning", TimeSpec(part=Part.early_morning)),
    ("night", TimeSpec(part=Part.night)),
    ("saturday", TimeSpec(day="weekday:sat")),
    ("saturday evening", TimeSpec(day="weekday:sat", part=Part.evening)),
    ("wednesday morning", TimeSpec(day="weekday:wed", part=Part.morning)),
    ("sunday midday", TimeSpec(day="weekday:sun", part=Part.midday)),
    ("this weekend", TimeSpec(day="this_weekend")),
    ("this weekend morning", TimeSpec(day="this_weekend", part=Part.morning)),
    ("next weekend", TimeSpec(day="next_weekend")),
    ("next 3 days", TimeSpec(day="next_n_days", n_days=3)),
    ("next few days evening", TimeSpec(day="next_n_days", part=Part.evening)),
    ("2026-10-26 afternoon", TimeSpec(day="date:2026-10-26", part=Part.afternoon)),
    ("2026-10-20 (past)", TimeSpec(day="date:2026-10-20")),
    ("2026-11-05 (far)", TimeSpec(day="date:2026-11-05")),
    ("7 baje", TimeSpec(hour=7)),
    ("tomorrow evening 7", TimeSpec(day="tomorrow", part=Part.evening, hour=7)),
    ("tomorrow around sunrise", TimeSpec(day="tomorrow", around="sunrise")),
    ("around sunset", TimeSpec(around="sunset")),
    ("tomorrow, any time", TimeSpec(day="tomorrow", part=Part.any)),
]


def sun_times(start: date, days: int) -> tuple[dict[date, datetime], dict[date, datetime]]:
    rise, set_ = {}, {}
    for k in range(days):
        d = start + timedelta(k)
        s = sun(POWAI.observer, date=d, tzinfo=IST)
        rise[d], set_[d] = s["sunrise"], s["sunset"]
    return rise, set_


def compact(starts: list[datetime]) -> str:
    """'Thu 22 06-10 | Fri 23 06-10': hour ranges per day."""
    parts = []
    for d, group in groupby(starts, key=lambda s: s.date()):
        runs: list[list[int]] = []
        for h in (s.hour for s in group):
            if runs and h == runs[-1][-1] + 1:
                runs[-1].append(h)
            else:
                runs.append([h])
        text = ",".join(f"{r[0]:02d}" if len(r) == 1 else f"{r[0]:02d}-{r[-1]:02d}" for r in runs)
        parts.append(f"{d:%a} {d.day} {text}")
    return " | ".join(parts) or "—"


def main() -> None:
    for now in NOWS:
        rise, set_ = sun_times(now.date(), 7)
        print(f"\nnow = {now:%a %d %b %Y %H:%M} IST   "
              f"(sunrise {rise[now.date()]:%H:%M}, sunset {set_[now.date()]:%H:%M})")  # fmt: skip
        print(f"{'spec':24} {'n':>3}  {'label':38} starts")
        print("-" * 120)
        for name, spec in SPECS:
            win = resolve_window(spec, now, rise, set_)
            assert all(s >= now for s in win.starts), name
            print(f"{name:24} {len(win.starts):3d}  {win.label[:38]:38} {compact(win.starts)}")
            for a in win.assumptions:
                print(f"{'':30}↳ {a}")


if __name__ == "__main__":
    main()
