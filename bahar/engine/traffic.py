"""Traffic heuristic (no free live traffic exists for India; disclosed in the README)."""

from datetime import datetime

from bahar.config import weights as w


def travel_multiplier(t: datetime) -> float:
    """OSRM free-flow duration multiplier for a departure at IST time t."""
    if t.weekday() in w.WEEKEND_DAYS:
        return w.TRAVEL_WEEKEND_DAY if t.hour in w.WEEKEND_DAYTIME_HOURS else 1.0
    if t.hour in w.WEEKDAY_PEAK_HOURS:
        return w.TRAVEL_PEAK
    if t.hour in w.WEEKDAY_SHOULDER_HOURS:
        return w.TRAVEL_SHOULDER
    return 1.0


def road_traffic_factor(t: datetime) -> float:
    """Road-emission factor for the exposure index: peak = hours whose travel multiplier is
    the peak value, night = 23:00-06:00, otherwise 1."""
    if travel_multiplier(t) == w.TRAVEL_PEAK:
        return w.ROAD_PEAK_FACTOR
    if t.hour in w.ROAD_NIGHT_HOURS:
        return w.ROAD_NIGHT_FACTOR
    return 1.0
