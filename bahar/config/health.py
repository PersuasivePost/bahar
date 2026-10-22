"""Health / age profiles (CLAUDE.md profile table). When several match, the strictest applies.

AQ limits are on session-mean local PM2.5 (µg/m³). The default 90 is the top of NAQI
"Moderate"; sensitive groups stop at 60, the top of "Satisfactory" — CPCB's NAQI health
statements flag Moderate air as causing breathing discomfort for people with lung and heart
disease, children and older adults. High-intensity sessions roughly double or triple minute
ventilation, hence the tighter second limit.
WBGT stops follow the ACSM position stand on exertional heat illness (2007) and the
heat-stress guidance of Sports Medicine Australia: cancel strenuous activity around 30-32 °C
for healthy adults, ~2-4 °C lower for people with reduced heat tolerance.
"""

from typing import Final

from pydantic import BaseModel, ConfigDict

from bahar.models import Condition, Profile

DISCLAIMER: Final = "Not medical advice — follow your doctor's guidance."
UV_THRESHOLD_DEFAULT: Final = 8.0  # WHO UV index: 8-10 "very high"
UV_THRESHOLD_CHILD: Final = 6.0  # WHO: children need protection from "high" (6-7) upward


class HealthProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    pm25_limit: float  # normal / moderate intensity
    pm25_limit_high: float  # high intensity
    wbgt_stop: float
    wbgt_stop_high: float
    uv_threshold: float
    extras: frozenset[str] = frozenset()  # advice codes added by this profile
    disclaimer: bool = True


PROFILES: Final[dict[str, HealthProfile]] = {
    p.key: p
    for p in (
        HealthProfile(
            key="default", pm25_limit=90, pm25_limit_high=60, wbgt_stop=32, wbgt_stop_high=30,
            uv_threshold=UV_THRESHOLD_DEFAULT, disclaimer=False,
        ),
        HealthProfile(
            key="asthma_copd", pm25_limit=60, pm25_limit_high=45, wbgt_stop=30,
            wbgt_stop_high=30, uv_threshold=UV_THRESHOLD_DEFAULT,
            extras=frozenset({"inhaler", "warm_up", "avoid_near_road_hours"}),
        ),
        HealthProfile(
            key="heart", pm25_limit=60, pm25_limit_high=45, wbgt_stop=28, wbgt_stop_high=28,
            uv_threshold=UV_THRESHOLD_DEFAULT,
            extras=frozenset({"avoid_high_intensity_in_heat"}),
        ),
        HealthProfile(
            key="senior", pm25_limit=60, pm25_limit_high=45, wbgt_stop=28, wbgt_stop_high=28,
            uv_threshold=UV_THRESHOLD_DEFAULT,
            extras=frozenset({"prefer_flat_shaded_close"}),
        ),
        HealthProfile(
            key="child", pm25_limit=60, pm25_limit_high=45, wbgt_stop=28, wbgt_stop_high=28,
            uv_threshold=UV_THRESHOLD_CHILD, extras=frozenset({"shorter_sessions"}),
        ),
        HealthProfile(
            key="pregnancy", pm25_limit=60, pm25_limit_high=45, wbgt_stop=28,
            wbgt_stop_high=28, uv_threshold=UV_THRESHOLD_DEFAULT,
            extras=frozenset({"avoid_high_intensity_in_heat", "hydration"}),
        ),
    )
}  # fmt: skip

_CONDITION_PROFILE: Final = {
    Condition.asthma: "asthma_copd",
    Condition.copd: "asthma_copd",
    Condition.heart: "heart",
    Condition.pregnancy: "pregnancy",
    Condition.other: "asthma_copd",  # cautious default for an unrecognised condition
}


def resolve_health(profile: Profile) -> tuple[HealthProfile, list[str]]:
    """Merge every matching profile into one, taking the strictest value of each limit and
    the union of extras. Returns the merged profile and the assumptions it implies."""
    keys = {_CONDITION_PROFILE[c] for c in profile.conditions}
    if profile.age_group in ("child", "senior"):
        keys.add(profile.age_group)
    assumptions = []
    if Condition.other in profile.conditions:
        assumptions.append("unrecognised health condition: applied asthma/COPD limits")
    matched = [PROFILES[k] for k in sorted(keys)] or [PROFILES["default"]]
    if len(matched) == 1:
        return matched[0], assumptions
    merged = HealthProfile(
        key="+".join(p.key for p in matched),
        pm25_limit=min(p.pm25_limit for p in matched),
        pm25_limit_high=min(p.pm25_limit_high for p in matched),
        wbgt_stop=min(p.wbgt_stop for p in matched),
        wbgt_stop_high=min(p.wbgt_stop_high for p in matched),
        uv_threshold=min(p.uv_threshold for p in matched),
        extras=frozenset().union(*(p.extras for p in matched)),
        disclaimer=any(p.disclaimer for p in matched),
    )
    return merged, assumptions
