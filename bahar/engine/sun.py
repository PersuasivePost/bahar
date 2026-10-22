"""Sun position and daylight windows (astral, computed locally: no network)."""

from collections.abc import Iterable
from datetime import date, datetime
from typing import Literal, NamedTuple

from astral import Observer
from astral.sun import (
    SunDirection,
    azimuth,
    blue_hour,
    elevation,
    golden_hour,
    sunrise,
    sunset,
)

from bahar.config import weights as w
from bahar.config.sources import IST

Light = Literal["day", "golden", "blue", "dark"]
Span = tuple[datetime, datetime]


class SunDay(NamedTuple):
    day: date
    sunrise: datetime
    sunset: datetime
    golden: tuple[Span, Span]  # morning, evening (sun elevation -4..6 deg)
    blue: tuple[Span, Span]  # morning, evening (-6..-4 deg)


def sun_day(lat: float, lon: float, d: date) -> SunDay:
    o = Observer(lat, lon)
    rising, setting = SunDirection.RISING, SunDirection.SETTING
    return SunDay(
        day=d,
        sunrise=sunrise(o, d, tzinfo=IST),
        sunset=sunset(o, d, tzinfo=IST),
        golden=(golden_hour(o, d, rising, tzinfo=IST), golden_hour(o, d, setting, tzinfo=IST)),
        blue=(blue_hour(o, d, rising, tzinfo=IST), blue_hour(o, d, setting, tzinfo=IST)),
    )


def sun_days(lat: float, lon: float, days: Iterable[date]) -> dict[date, SunDay]:
    return {d: sun_day(lat, lon, d) for d in sorted(set(days))}


def sun_position(lat: float, lon: float, t: datetime) -> tuple[float, float]:
    """(elevation, azimuth) in degrees at time t; elevation is refraction-corrected."""
    o = Observer(lat, lon)
    return elevation(o, t), azimuth(o, t)


def light_class(elev: float) -> Light:
    if elev > w.GOLDEN_ELEV[1]:
        return "day"
    if elev >= w.GOLDEN_ELEV[0]:
        return "golden"
    if elev >= w.BLUE_ELEV[0]:
        return "blue"
    return "dark"


def overlap_min(a: Span, b: Span) -> float:
    """Minutes two time spans overlap (0 if disjoint)."""
    start, end = max(a[0], b[0]), min(a[1], b[1])
    return max(0.0, (end - start).total_seconds() / 60)
