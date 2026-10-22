"""Venue discovery tables: Overpass selectors per activity and candidate selection knobs.

Mirrors the CLAUDE.md activity table; folded into config/activities.py in Step 3.
"""

from typing import Final

from bahar.models import Activity


def _and(*filters: str) -> str:
    """AND-combine tag filters into the inner text of one Overpass `[...]` clause chain."""
    return "][".join(filters)


_PARK = 'leisure~"^(park|garden)$"'
_REC = "landuse=recreation_ground"
_BEACH = "natural=beach"

# Entries are OR-ed; tags inside an entry (built with _and) are AND-ed.
_RUN_WALK = (
    'leisure~"^(park|garden|track|nature_reserve)$"',
    _REC,
    _BEACH,
)

VENUE_SELECTORS: Final[dict[Activity, tuple[str, ...]]] = {
    Activity.running: _RUN_WALK,
    Activity.walking: _RUN_WALK,
    Activity.cycling: (_PARK, _REC, "highway=cycleway"),
    Activity.golf: ("leisure=golf_course",),
    Activity.cricket: (_and('leisure~"^(pitch|stadium)$"', "sport=cricket"), _REC),
    Activity.football: (_and('leisure~"^(pitch|stadium)$"', "sport=soccer"),),
    Activity.tennis: (_and("leisure=pitch", "sport=tennis", "indoor!=yes"),),
    Activity.hiking: (
        "leisure=nature_reserve",
        'boundary~"^(protected_area|national_park)$"',
        "natural=peak",
        "historic=fort",
        "tourism=viewpoint",
    ),
    Activity.picnic: (_PARK, "tourism=picnic_site", _BEACH, _REC),
    Activity.event: (_PARK, _REC, "amenity=events_venue"),
    Activity.photography: (
        "tourism=viewpoint",
        _BEACH,
        "historic",
        'man_made~"^(pier|lighthouse)$"',
        "leisure=park",
    ),
}

# Default search radius (km) per activity.
DEFAULT_RADIUS_KM: Final[dict[Activity, float]] = {
    Activity.running: 5, Activity.walking: 5, Activity.cycling: 10, Activity.golf: 40,
    Activity.cricket: 10, Activity.football: 8, Activity.tennis: 8, Activity.hiking: 40,
    Activity.picnic: 15, Activity.event: 15, Activity.photography: 10,
}  # fmt: skip

# ---- candidate selection
MAX_CANDIDATES: Final = 12
EXCLUDED_LEISURE: Final = frozenset({"dog_park", "playground"})

# bbox area floors in km2. Parks/gardens used for these activities must be at least
# PARK_MIN_AREA_KM2 (~2 ha: room for a real session); points have unknown size and are kept
# but rank below polygons. Other area features only need MIN_AREA_KM2. Tracks, pitches and
# courts are small by nature and exempt from both.
PARK_ACTIVITIES: Final = frozenset(
    {Activity.running, Activity.walking, Activity.cycling, Activity.picnic, Activity.event}
)
PARK_KINDS: Final = frozenset({"park", "garden"})
PARK_MIN_AREA_KM2: Final = 0.02
MIN_AREA_KM2: Final = 0.005
AREA_EXEMPT_KINDS: Final = frozenset({"pitch", "stadium", "track", "golf_course"})
POINT_RANK_PENALTY: Final = 1.0  # added to the rank of point-mapped parks/gardens

# ---- duplicate merging (same place mapped twice, or under different names)
MERGE_DISTANCE_M: Final = 300
MERGE_DISTANCE_WIDE_M: Final = 1500  # forts / peaks / viewpoints: mapped far apart, e.g. summit vs fort
WIDE_KINDS: Final = frozenset({"fort", "peak", "viewpoint"})
MERGE_NAME_RATIO: Final = 0.8
POINT_IN_BBOX_MARGIN_M: Final = 100
GENERIC_WORDS: Final = frozenset(
    {"fort", "killa", "kila", "gad", "garden", "gardens", "park", "point", "peak"}
)
GENERIC_SUFFIXES: Final = ("gad", "garh", "killa", "kila", "fort")  # glued to a name: Lohgad

# Lower rank is better: distance as a share of the radius, minus a bonus for size.
RANK_SIZE_BONUS: Final = 0.5
RANK_SIZE_SATURATION_KM2: Final = 0.5
PRIVATE_ACCESS: Final = frozenset({"private", "members", "customers", "no"})
