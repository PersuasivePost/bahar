"""TimeSpec -> candidate hourly session starts in IST. Pure: `now` and sun times are passed in."""

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta

from bahar.config import weights as w
from bahar.config.sources import FORECAST_DAYS, IST
from bahar.models import Part, TimeSpec, TimeWindow

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_PART_LABEL = {p: p.value.replace("_", " ") for p in Part}
_SAT = 5

SunTimes = Mapping[date, datetime]


def _fmt_day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _days_label(days: list[date], today: date) -> str:
    if len(days) == 1:
        named = {today: "today", today + timedelta(1): "tomorrow"}.get(days[0])
        return f"{named} ({_fmt_day(days[0])})" if named else _fmt_day(days[0])
    return f"{_fmt_day(days[0])} – {_fmt_day(days[-1])}"


def _weekend(today: date, skip_current: bool) -> list[date]:
    """Sat + Sun of the coming weekend (the current one if today is Sat/Sun), or of the
    weekend after that when skip_current. A Sunday's current weekend is only that Sunday."""
    sat = today + timedelta((_SAT - today.weekday()) % 7)
    if today.weekday() == 6:  # Sunday: this weekend's Saturday is yesterday
        sat = today - timedelta(1)
    if skip_current:
        sat += timedelta(7)
    return [d for d in (sat, sat + timedelta(1)) if d >= today]


def _sun_event(d: date, times: SunTimes | None, fallback: tuple[int, int]) -> tuple[datetime, bool]:
    """(event time, estimated?) for day d."""
    if times and d in times:
        return times[d].astimezone(IST), False
    return datetime.combine(d, time(*fallback), IST), True


def _start_hours(
    spec: TimeSpec, sunrise: SunTimes | None, sunset: SunTimes | None, assumptions: list[str]
) -> tuple[Callable[[date], list[datetime]], str]:
    """Per-day start-hour generator for the time-of-day part of the spec, plus its label.
    Precedence: around sunrise/sunset > explicit hour > part of day > whole day."""
    if spec.around:
        is_rise = spec.around == "sunrise"
        times = sunrise if is_rise else sunset
        fallback = w.FALLBACK_SUNRISE if is_rise else w.FALLBACK_SUNSET

        def around(d: date) -> list[datetime]:
            event, estimated = _sun_event(d, times, fallback)
            if estimated:
                note = f"{spec.around} time not available: assumed {event:%H:%M} IST"
                if note not in assumptions:
                    assumptions.append(note)
            lo = event - timedelta(minutes=w.AROUND_SUN_BEFORE_MIN)
            hi = event + timedelta(minutes=w.AROUND_SUN_AFTER_MIN)
            first = lo.replace(minute=0, second=0, microsecond=0)
            if first < lo:
                first += timedelta(hours=1)
            n = int((hi - first) / timedelta(hours=1)) + 1
            return [first + timedelta(hours=k) for k in range(n)]

        return around, f"around {spec.around}"

    if spec.hour is not None:
        hour = spec.hour
        if spec.part and hour < 12:
            part_hours = w.PART_START_HOURS[spec.part]
            if hour not in part_hours and hour + 12 in part_hours:  # "evening 7 baje" -> 7
                hour += 12
                assumptions.append(f"read {spec.hour}:00 {_PART_LABEL[spec.part]} as {hour}:00")
        slack = w.EXPLICIT_HOUR_SLACK_H
        hours = [h for h in range(hour - slack, hour + slack + 1) if 0 <= h <= 23]
        return lambda d: [datetime.combine(d, time(h), IST) for h in hours], f"around {hour:02d}:00"

    part = spec.part or Part.any
    hours = sorted(w.PART_START_HOURS[part])
    label = "" if part is Part.any else _PART_LABEL[part]
    return lambda d: [datetime.combine(d, time(h), IST) for h in hours], label


def _days(spec: TimeSpec, today: date, assumptions: list[str]) -> tuple[list[date], bool]:
    """Requested days and whether an exhausted single day may roll forward
    (today -> tomorrow, today's weekday -> next week)."""
    day = spec.day
    if day in (None, "today"):
        return [today], True
    if day == "tomorrow":
        return [today + timedelta(1)], False
    if day == "day_after":
        return [today + timedelta(2)], False
    if day == "this_weekend":
        return _weekend(today, skip_current=False), False
    if day == "next_weekend":
        if today.weekday() < _SAT:
            assumptions.append("'next weekend' read as the coming weekend")
            return _weekend(today, skip_current=False), False
        return _weekend(today, skip_current=True), False
    if day.startswith("weekday:") and day[8:11] in _WEEKDAYS:
        target = _WEEKDAYS.index(day[8:11])
        return [today + timedelta((target - today.weekday()) % 7)], True
    if day.startswith("date:"):
        try:
            return [date.fromisoformat(day[5:])], False
        except ValueError:
            pass
    assumptions.append(f"unrecognised day '{day}': using today")
    return [today], True


def resolve_window(
    spec: TimeSpec,
    now: datetime,
    sunrise: SunTimes | None = None,
    sunset: SunTimes | None = None,
) -> TimeWindow:
    """Resolve a parsed time spec into hourly session starts (IST), never in the past and
    never beyond the forecast horizon (today + FORECAST_DAYS - 1). An empty `starts` means
    the requested time is past or out of range; the assumptions say which."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(IST)
    today = now.date()
    earliest = now.replace(minute=0, second=0, microsecond=0)
    if earliest < now:
        earliest += timedelta(hours=1)
    horizon = datetime.combine(today + timedelta(FORECAST_DAYS), time(0), IST)
    assumptions: list[str] = []

    if spec.now:
        first = now.replace(minute=0, second=0, microsecond=0)
        starts = [first + timedelta(hours=k) for k in range(w.NOW_WINDOW_H + 1)]
        return _finish(starts, f"now (next {w.NOW_WINDOW_H} hours)", horizon, assumptions)

    has_time_of_day = spec.part not in (None, Part.any) or spec.hour is not None or spec.around
    if spec.day is None and not has_time_of_day:
        if spec.part is None:
            assumptions.append(f"no time given: next {w.DEFAULT_WINDOW_H} hours")
        starts = [earliest + timedelta(hours=k) for k in range(w.DEFAULT_WINDOW_H)]
        return _finish(starts, f"next {w.DEFAULT_WINDOW_H} hours", horizon, assumptions)

    per_day, tod_label = _start_hours(spec, sunrise, sunset, assumptions)

    if spec.day == "next_n_days":
        n = spec.n_days or w.DEFAULT_N_DAYS
        if spec.n_days is None:
            assumptions.append(f"number of days not given: next {n} days")
        end = now + timedelta(days=n)
        days = [today + timedelta(k) for k in range(n + 1)]
        starts = [s for d in days for s in per_day(d) if earliest <= s < end]
        label = f"next {n} days" + (f", {tod_label}" if tod_label else "")
        return _finish(starts, label, horizon, assumptions)

    days, can_roll = _days(spec, today, assumptions)
    starts = [s for d in days for s in per_day(d) if s >= earliest]
    if not starts and can_roll and len(days) == 1:
        step = 1 if spec.day in (None, "today") else 7
        rolled = days[0] + timedelta(step)
        what = tod_label or "today"
        assumptions.append(f"{what} has already passed: using {_fmt_day(rolled)}")
        days = [rolled]
        starts = [s for s in per_day(rolled) if s >= earliest]
    if not starts and days[-1] < today:
        assumptions.append(f"{_fmt_day(days[-1])} is in the past")

    kept_days = sorted({s.date() for s in starts if s < horizon}) or days
    label = _days_label(kept_days, today) + (f", {tod_label}" if tod_label else "")
    return _finish(starts, label, horizon, assumptions)


def _finish(
    starts: list[datetime], label: str, horizon: datetime, assumptions: list[str]
) -> TimeWindow:
    kept = sorted({s for s in starts if s < horizon})
    if len(kept) < len(set(starts)):
        assumptions.append(
            f"{len(set(starts)) - len(kept)} start hours beyond the "
            f"{FORECAST_DAYS}-day forecast dropped"
        )
    return TimeWindow(starts=kept, label=label, assumptions=assumptions)
