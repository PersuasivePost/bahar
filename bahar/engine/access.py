"""Minimal OSM `opening_hours` parser: `24/7`, `Mo-Su 05:00-21:00`, day lists and ranges,
several rules separated by `;` (a later rule replaces the days it names), comma-separated
time spans, spans past midnight, `sunrise-sunset`, `off` / `closed`. Rules for public
holidays (`PH`) are skipped (holidays are not modelled). Anything else is unparseable: the
caller treats the venue as open and records an assumption."""

import re
from datetime import datetime, timedelta
from typing import Literal, NamedTuple

from bahar.engine.sun import SunDay

_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_DAY_SPEC = re.compile(r"^(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?$")
_TIME = re.compile(r"^(?:(\d{1,2}):(\d{2})|(sunrise|sunset))$")

TimeRef = int | Literal["sunrise", "sunset"]  # minutes after midnight, or a sun event


class Span(NamedTuple):
    start: TimeRef
    end: TimeRef


class OpeningHours(NamedTuple):
    raw: str
    week: dict[int, list[Span]]  # weekday (Mon = 0) -> open spans; missing / [] = closed


def _days(spec: str) -> list[int] | None:
    out: list[int] = []
    for part in spec.split(","):
        m = _DAY_SPEC.match(part.strip())
        if not m:
            return None
        a = _DAYS.index(m.group(1))
        b = _DAYS.index(m.group(2)) if m.group(2) else a
        out += [d % 7 for d in range(a, b + 1 if b >= a else b + 8)]  # Fr-Mo wraps
    return out


def _time(text: str) -> TimeRef | None:
    m = _TIME.match(text.strip())
    if not m:
        return None
    if m.group(3):
        return m.group(3)  # type: ignore[return-value]
    hour, minute = int(m.group(1)), int(m.group(2))
    return hour * 60 + minute if hour <= 24 and minute < 60 else None


def _spans(text: str) -> list[Span] | None:
    out = []
    for part in text.split(","):
        ends = part.strip().split("-")
        if len(ends) != 2:
            return None
        start, end = _time(ends[0]), _time(ends[1])
        if start is None or end is None:
            return None
        out.append(Span(start, end))
    return out


def parse_opening_hours(raw: str) -> OpeningHours | None:
    text = raw.strip()
    if text == "24/7":
        return OpeningHours(raw, {d: [Span(0, 24 * 60)] for d in range(7)})
    week: dict[int, list[Span]] = {}
    for rule in filter(None, (r.strip() for r in text.split(";"))):
        if "PH" in rule:
            continue
        head, _, rest = rule.partition(" ")
        days = _days(head)
        if days is None:  # no day spec: the rule covers every day
            days, rest = list(range(7)), rule
        rest = rest.strip()
        if rest in ("off", "closed"):
            spans: list[Span] | None = []
        elif rest in ("", "24/7"):
            spans = [Span(0, 24 * 60)]
        else:
            spans = _spans(rest)
        if spans is None:
            return None
        for d in days:
            week[d] = spans
    return OpeningHours(raw, week) if week else None


def _resolve(ref: TimeRef, sun: SunDay) -> int:
    if ref == "sunrise":
        return sun.sunrise.hour * 60 + sun.sunrise.minute
    if ref == "sunset":
        return sun.sunset.hour * 60 + sun.sunset.minute
    return ref


def is_open(oh: OpeningHours, start: datetime, end: datetime, sun: SunDay) -> bool:
    """True if the whole session [start, end) lies inside one open span of start's day.
    A span whose end is earlier than its start runs past midnight."""
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    s = (start - day_start) / timedelta(minutes=1)
    e = (end - day_start) / timedelta(minutes=1)
    for span in oh.week.get(start.weekday(), []):
        a, b = _resolve(span.start, sun), _resolve(span.end, sun)
        if b <= a:
            b += 24 * 60
        if a <= s and e <= b:
            return True
    return False
