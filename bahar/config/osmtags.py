"""OSM tag tables shared by the live Overpass queries and the local OSM index, so both select
exactly the same features."""

import re
from typing import Final

ROAD_CLASSES: Final = ("motorway", "trunk", "primary", "secondary")
INDUSTRY_CLASSES: Final = ("industrial", "landfill", "construction", "quarry")
GREEN_VALUES: Final = ("forest", "grass", "meadow", "village_green", "recreation_ground", "park",
                       "garden", "nature_reserve", "wood", "scrub", "grassland")  # fmt: skip
GREEN_KEYS: Final = ("landuse", "leisure", "natural")
FOSSIL: Final = ("coal", "gas", "oil", "diesel", "lignite", "petroleum")
WATER_NATURAL: Final = ("water", "wetland", "coastline")
WATER_WAYS: Final = ("river", "canal")
_FOSSIL_RE: Final = re.compile("|".join(FOSSIL))


def classify(tags: dict[str, str]) -> tuple[str, str]:
    """(group, cls) for the first matching rule; anything a context query returned that matches
    no other rule matched the green regex."""
    if tags.get("highway") in ROAD_CLASSES:
        return "road", tags["highway"]
    if tags.get("landuse") in INDUSTRY_CLASSES:
        return "industry", tags["landuse"]
    if tags.get("power") == "plant":
        return "power", "power_plant"
    if tags.get("natural") == "coastline":
        return "water", "coastline"
    if tags.get("natural") in ("water", "wetland"):
        return "water", "water"
    if tags.get("waterway") in WATER_WAYS:
        return "water", "river"
    return "green", "green"


def context_scope(tags: dict[str, str], is_way: bool) -> str | None:
    """Which context query a feature belongs to - "far" (roads, industry, power: 2 km) or "near"
    (green, water: 500 m) - or None. Mirrors the clauses of overpass._context_query."""
    if is_way and tags.get("highway") in ROAD_CLASSES:
        return "far"
    if tags.get("landuse") in INDUSTRY_CLASSES:
        return "far"
    if tags.get("power") == "plant" and _FOSSIL_RE.search(tags.get("plant:source", "")):
        return "far"
    if any(tags.get(k) in GREEN_VALUES for k in GREEN_KEYS):
        return "near"
    if tags.get("natural") in WATER_NATURAL or (is_way and tags.get("waterway") in WATER_WAYS):
        return "near"
    return None


def osmium_context_filters() -> list[str]:
    """`osmium tags-filter` expressions that cover context_scope (a superset; exact matching
    happens in Python)."""
    return [
        f"w/highway={','.join(ROAD_CLASSES)}",
        f"nwr/landuse={','.join(INDUSTRY_CLASSES)}",
        "nwr/power=plant",
        *(f"nwr/{k}={','.join(GREEN_VALUES)}" for k in GREEN_KEYS),
        f"nwr/natural={','.join(WATER_NATURAL)}",
        f"w/waterway={','.join(WATER_WAYS)}",
    ]
