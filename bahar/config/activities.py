"""Activity profiles (CLAUDE.md activity table), venue discovery tables and multilingual synonyms.

Single source of truth for everything activity-specific: Overpass selectors, radii, session
length, intensity, weather limits, light rule, and the venue-candidate selection knobs.
"""

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from bahar.models import Activity, Lang

Intensity = Literal["low", "moderate", "high"]
# dusk_ok: until sunset + DUSK_GRACE_MIN (weights.py) · daylight: sunrise..sunset ·
# daylight_or_lit: daylight, or any time at a venue tagged lit=yes · back_before_sunset: the
# whole session must end by sunset · evening_ok: until the night part of day ends ·
# golden_bonus: any light, golden / blue hour scores a bonus.
LightRule = Literal[
    "dusk_ok", "daylight", "daylight_or_lit", "back_before_sunset", "evening_ok", "golden_bonus"
]


class ActivityProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: Activity
    venue_selectors: tuple[str, ...]  # Overpass tag filters, OR-ed (see _and)
    radius_km: float
    duration_min: int
    intensity: Intensity
    # Approximate adult minute ventilation; aligned with the activity levels of the US EPA
    # Exposure Factors Handbook (2011), ch. 6 (sedentary ~8, light ~12-20, moderate ~25-35,
    # high ~45-55 L/min).
    ventilation_lpm: float
    wind_max_kmh: float  # sustained
    gust_max_kmh: float
    rain_prob_max: float  # %
    light: LightRule
    # Parks / gardens must be at least PARK_MIN_AREA_KM2 for activities that need room.
    park_area_floor: bool
    synonyms: dict[Lang, tuple[str, ...]]


def _and(*filters: str) -> str:
    """AND-combine tag filters into the inner text of one Overpass `[...]` clause chain."""
    return "][".join(filters)


_PARK = 'leisure~"^(park|garden)$"'
_REC = "landuse=recreation_ground"
_BEACH = "natural=beach"
_RUN_WALK = ('leisure~"^(park|garden|track|nature_reserve)$"', _REC, _BEACH)

# Synonyms are matched case-insensitively on word boundaries (see normalize_activity).
ACTIVITIES: Final[dict[Activity, ActivityProfile]] = {
    p.key: p
    for p in (
        ActivityProfile(
            key=Activity.running, venue_selectors=_RUN_WALK, radius_km=5, duration_min=45,
            intensity="high", ventilation_lpm=55, wind_max_kmh=35, gust_max_kmh=50,
            rain_prob_max=40, light="dusk_ok", park_area_floor=True,
            synonyms={
                Lang.en: ("run", "running", "jog", "jogging", "sprint", "5k", "10k"),
                Lang.hinglish: ("daud", "daudna", "daudne", "dodna", "jogging karna"),
                Lang.hi: ("दौड़", "दौड़ना", "दौड़ने", "जॉगिंग", "रनिंग"),
                Lang.mr: ("धावणे", "धावायला", "धावण्यासाठी", "पळायला", "जॉगिंग"),
            },
        ),
        ActivityProfile(
            key=Activity.walking, venue_selectors=_RUN_WALK, radius_km=5, duration_min=45,
            intensity="low", ventilation_lpm=20, wind_max_kmh=40, gust_max_kmh=60,
            rain_prob_max=40, light="dusk_ok", park_area_floor=True,
            synonyms={
                Lang.en: ("walk", "walking", "stroll", "morning walk", "evening walk"),
                Lang.hinglish: ("tehelna", "tahalna", "tehelne", "ghumna", "ghoomne", "sair"),
                Lang.hi: ("टहलना", "टहलने", "सैर", "वॉक", "घूमने"),
                Lang.mr: ("चालणे", "चालायला", "फिरायला", "फेरफटका"),
            },
        ),
        ActivityProfile(
            key=Activity.cycling, venue_selectors=(_PARK, _REC, "highway=cycleway"),
            radius_km=10, duration_min=60, intensity="moderate", ventilation_lpm=35,
            wind_max_kmh=25, gust_max_kmh=40, rain_prob_max=30, light="dusk_ok",
            park_area_floor=True,
            synonyms={
                Lang.en: ("cycle", "cycling", "bike ride", "biking", "bicycle"),
                Lang.hinglish: ("cycle chalana", "cycle chalane", "saikil", "cycling karna"),
                Lang.hi: ("साइकिल", "साइकिलिंग", "साइकिल चलाना"),
                Lang.mr: ("सायकल", "सायकलिंग", "सायकल चालवायला"),
            },
        ),
        ActivityProfile(
            key=Activity.golf, venue_selectors=("leisure=golf_course",), radius_km=40,
            duration_min=240, intensity="low", ventilation_lpm=20, wind_max_kmh=25,
            gust_max_kmh=40, rain_prob_max=30, light="daylight", park_area_floor=False,
            synonyms={
                Lang.en: ("golf", "golfing", "tee time", "round of golf"),
                Lang.hinglish: ("golf khelna", "golf khelne"),
                Lang.hi: ("गोल्फ",),
                Lang.mr: ("गोल्फ",),
            },
        ),
        ActivityProfile(
            key=Activity.cricket,
            venue_selectors=(_and('leisure~"^(pitch|stadium)$"', "sport=cricket"), _REC),
            radius_km=10, duration_min=180, intensity="moderate", ventilation_lpm=25,
            wind_max_kmh=30, gust_max_kmh=45, rain_prob_max=30, light="daylight",
            park_area_floor=False,
            synonyms={
                Lang.en: ("cricket", "nets", "net practice", "box cricket"),
                Lang.hinglish: ("cricket khelna", "cricket khelne", "match khelna"),
                Lang.hi: ("क्रिकेट",),
                Lang.mr: ("क्रिकेट",),
            },
        ),
        ActivityProfile(
            key=Activity.football,
            venue_selectors=(_and('leisure~"^(pitch|stadium)$"', "sport=soccer"),),
            radius_km=8, duration_min=90, intensity="high", ventilation_lpm=50,
            wind_max_kmh=35, gust_max_kmh=50, rain_prob_max=40, light="daylight_or_lit",
            park_area_floor=False,
            synonyms={
                Lang.en: ("football", "soccer", "futsal", "turf", "5-a-side"),
                Lang.hinglish: ("football khelna", "football khelne"),
                Lang.hi: ("फुटबॉल",),
                Lang.mr: ("फुटबॉल",),
            },
        ),
        ActivityProfile(
            key=Activity.tennis,
            venue_selectors=(_and("leisure=pitch", "sport=tennis", "indoor!=yes"),),
            radius_km=8, duration_min=90, intensity="high", ventilation_lpm=45,
            wind_max_kmh=20, gust_max_kmh=30, rain_prob_max=30, light="daylight_or_lit",
            park_area_floor=False,
            synonyms={
                Lang.en: ("tennis", "tennis court"),
                Lang.hinglish: ("tennis khelna", "tennis khelne"),
                Lang.hi: ("टेनिस",),
                Lang.mr: ("टेनिस",),
            },
        ),
        ActivityProfile(
            key=Activity.hiking,
            venue_selectors=(
                "leisure=nature_reserve",
                'boundary~"^(protected_area|national_park)$"',
                "natural=peak",
                "historic=fort",
                "tourism=viewpoint",
            ),
            radius_km=40, duration_min=240, intensity="moderate", ventilation_lpm=35,
            wind_max_kmh=40, gust_max_kmh=60, rain_prob_max=30, light="back_before_sunset",
            park_area_floor=False,
            synonyms={
                Lang.en: ("hike", "hiking", "trek", "trekking", "trail", "fort climb"),
                Lang.hinglish: ("pahad chadhna", "pahadi", "trek karna", "killa chadhna"),
                Lang.hi: ("ट्रेक", "ट्रेकिंग", "हाइकिंग", "पहाड़", "किला"),
                Lang.mr: ("ट्रेक", "ट्रेकिंग", "गड", "किल्ला", "डोंगर"),
            },
        ),
        ActivityProfile(
            key=Activity.picnic,
            venue_selectors=(_PARK, "tourism=picnic_site", _BEACH, _REC),
            radius_km=15, duration_min=180, intensity="low", ventilation_lpm=12,
            wind_max_kmh=30, gust_max_kmh=45, rain_prob_max=25, light="daylight",
            park_area_floor=True,
            synonyms={
                Lang.en: ("picnic", "outing", "day out", "family outing"),
                Lang.hinglish: ("picnic manana", "ghoomne jaana"),
                Lang.hi: ("पिकनिक", "सैर-सपाटा"),
                Lang.mr: ("पिकनिक", "सहल", "वनभोजन"),
            },
        ),
        ActivityProfile(
            key=Activity.event,
            venue_selectors=(_PARK, _REC, "amenity=events_venue"),
            radius_km=15, duration_min=240, intensity="low", ventilation_lpm=10,
            wind_max_kmh=30, gust_max_kmh=45, rain_prob_max=20, light="evening_ok",
            park_area_floor=True,
            synonyms={
                Lang.en: (
                    "event", "party", "birthday party", "gathering", "get-together",
                    "function", "meetup", "concert",
                ),
                Lang.hinglish: ("party karni", "function rakhna", "mela"),
                Lang.hi: ("कार्यक्रम", "इवेंट", "पार्टी", "समारोह"),
                Lang.mr: ("कार्यक्रम", "समारंभ", "पार्टी"),
            },
        ),
        ActivityProfile(
            key=Activity.photography,
            venue_selectors=(
                "tourism=viewpoint",
                _BEACH,
                "historic",
                'man_made~"^(pier|lighthouse)$"',
                "leisure=park",
            ),
            radius_km=10, duration_min=90, intensity="low", ventilation_lpm=12,
            wind_max_kmh=40, gust_max_kmh=60, rain_prob_max=50, light="golden_bonus",
            park_area_floor=False,
            synonyms={
                Lang.en: ("photo", "photos", "photography", "photoshoot", "photo walk", "shoot"),
                Lang.hinglish: ("photo khichna", "photo lena", "photos lene"),
                Lang.hi: ("फोटो", "फोटोग्राफी", "फोटोशूट", "तस्वीरें"),
                Lang.mr: ("फोटो", "फोटोग्राफी", "फोटो काढायला", "छायाचित्रण"),
            },
        ),
    )
}  # fmt: skip


def _synonym_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    # Longest first so "morning walk" wins over "walk". Python's \b is Unicode-aware but
    # Devanagari vowel signs are not \w, so use explicit "not a letter/mark" lookarounds.
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![\wऀ-ॿ])(?:{alts})(?![\wऀ-ॿ])", re.IGNORECASE)


_SYNONYM_PATTERNS: Final = {
    a: _synonym_pattern(tuple(w for ws in p.synonyms.values() for w in ws))
    for a, p in ACTIVITIES.items()
}


def normalize_activity(text: str) -> Activity | None:
    """Map free text (parser `activity_raw`) to an activity: the one whose longest synonym
    match is longest; None when nothing matches."""
    best: tuple[int, Activity] | None = None
    for activity, pattern in _SYNONYM_PATTERNS.items():
        m = pattern.search(text)
        if m and (best is None or len(m.group()) > best[0]):
            best = (len(m.group()), activity)
    return best[1] if best else None


# ---------------------------------------------------------------- candidate selection

MAX_CANDIDATES: Final = 12
EXCLUDED_LEISURE: Final = frozenset({"dog_park", "playground"})

# Telecom masts are often mapped as tourism=viewpoint / natural=peak ("Jio Tower Point").
NAME_BLOCKLIST_KINDS: Final = frozenset({"viewpoint", "peak"})
NAME_BLOCKLIST: Final = re.compile(
    r"network|jio|airtel|vodafone|tower|signal|antenna", re.IGNORECASE
)

# bbox area floors in km2. Parks/gardens used for park_area_floor activities must be at least
# PARK_MIN_AREA_KM2 (~2 ha: room for a real session); points have unknown size and are kept
# but rank below polygons. Other area features only need MIN_AREA_KM2. Tracks, pitches and
# courts are small by nature and exempt from both.
PARK_KINDS: Final = frozenset({"park", "garden"})
PARK_MIN_AREA_KM2: Final = 0.02
MIN_AREA_KM2: Final = 0.005
AREA_EXEMPT_KINDS: Final = frozenset({"pitch", "stadium", "track", "golf_course"})
POINT_RANK_PENALTY: Final = 1.0  # added to the rank of point-mapped parks/gardens

# ---- duplicate merging (same place mapped twice, or under different names)
MERGE_DISTANCE_M: Final = 300
# Forts / peaks / viewpoints are mapped far apart (summit vs fort), so merge them wider.
MERGE_DISTANCE_WIDE_M: Final = 1500
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
