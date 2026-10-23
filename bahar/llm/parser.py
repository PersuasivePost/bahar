"""SLM parser: free text -> ParsedQuery.

Grammar-constrained decoding (GBNF built from PARSER_SCHEMA) guarantees syntactically valid,
enum-correct JSON; `normalize` then applies deterministic fixes the model cannot be trusted
with (word lists in config/lexicon.py): activity synonyms, days / sun events / radius / health
conditions the user literally said, range clamps, the year of an explicit date, dropping place
names, age groups and "other" conditions the query does not support, and language detection.
All date maths stays in engine.timewindow.
"""

import json
import logging
import re
import time
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any, Final

from llama_cpp.llama_grammar import json_schema_to_gbnf

from bahar.config import lexicon as lx
from bahar.config.activities import (
    COMMUTE_ACTIVITIES,
    mentioned_activities,
    normalize_activity,
)
from bahar.config.sources import IST, MAX_RADIUS_KM, PLACE_RECOVERY_TRIES
from bahar.data.geocode import agrees, geocode
from bahar.llm.prompts import PARSER_PREFIX, PARSER_PREFIX_NAME, parser_suffix
from bahar.llm.runtime import LlmRuntime
from bahar.models import Activity, Condition, Lang, ParsedQuery, ParseResult, Part

log = logging.getLogger(__name__)

MAX_PARSE_TOKENS: Final = 160  # longest few-shot answer is ~75 tokens
MIN_RADIUS_KM: Final = 0.5
MAX_GROUP_SIZE: Final = 1000
MAX_PREFERENCES: Final = 4
# Word similarity for "is this place name in the query?" — tolerates transliteration and typos
# (lonavla ~ Lonavala = 0.93) while rejecting unrelated words.
PLACE_MATCH_RATIO: Final = 0.75
MIN_PLACE_WORD: Final = 3

_WEEKDAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAYS: Final = [
    "today", "tomorrow", "day_after", *(f"weekday:{d}" for d in _WEEKDAYS),
    "this_weekend", "next_weekend", "next_n_days",
]  # fmt: skip


def _text(max_len: int) -> dict[str, Any]:
    return {"type": "string", "maxLength": max_len}


# Only in_scope and lang are required; every other key is optional so the model omits unknowns
# (fewer generated tokens). The converter emits required keys first, then this order.
PARSER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "in_scope": {"type": "boolean"},
        "lang": {"enum": [x.value for x in Lang]},
        "activity": {"enum": [a.value for a in Activity]},
        "activity_raw": _text(40),
        "location_text": _text(60),
        "radius_km": {"type": "number"},
        "time": {
            "type": "object",
            "properties": {
                "day": {
                    "anyOf": [
                        {"enum": _DAYS},
                        {"type": "string", "pattern": r"^date:[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                    ]
                },
                "n_days": {"type": "integer"},
                "part": {"enum": [p.value for p in Part if p not in (Part.any, Part.after_work)]},
                "hour": {"type": "integer"},
                "around": {"enum": ["sunrise", "sunset"]},
            },
            "additionalProperties": False,
        },
        "profile": {
            "type": "object",
            "properties": {
                "age_group": {"enum": ["child", "adult", "senior"]},
                "conditions": {
                    "type": "array",
                    "items": {"enum": [c.value for c in Condition]},
                    "maxItems": 3,
                },
                "group_size": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "preferences": {"type": "array", "items": _text(30), "maxItems": MAX_PREFERENCES},
        "commute": {
            "type": "object",
            "properties": {"from": _text(60), "to": _text(60)},
            "additionalProperties": False,
        },
        # Last, so adding it left the decoding of every other key unchanged.
        "location_native": _text(60),
    },
    "required": ["in_scope", "lang"],
    "additionalProperties": False,
}
PARSER_GRAMMAR: Final = json_schema_to_gbnf(json.dumps(PARSER_SCHEMA))


def register_parser(runtime: LlmRuntime) -> int:
    """Evaluate and cache the parser prefix. Returns its token count."""
    return runtime.register_prefix(PARSER_PREFIX_NAME, PARSER_PREFIX)


async def parse_query(runtime: LlmRuntime, query: str, now: datetime) -> ParseResult:
    """Never raises: a missing model or unusable output falls back to the rule-based parse."""
    t0 = time.perf_counter()
    today = now.astimezone(IST).date()
    if not runtime.has_prefix(PARSER_PREFIX_NAME):
        return _fallback(query, today, "", "language model unavailable", t0)
    raw = ""
    try:
        raw = await runtime.complete(
            PARSER_PREFIX_NAME, parser_suffix(query), MAX_PARSE_TOKENS, PARSER_GRAMMAR
        )
        parsed, assumptions = normalize(json.loads(raw), query, today)
    except Exception as exc:  # truncated JSON, validation error, runtime failure
        log.warning("parser failed; raw=%r", raw, exc_info=True)
        return _fallback(query, today, raw, f"parser output unusable ({type(exc).__name__})", t0)
    return ParseResult(parsed=parsed, raw=raw, assumptions=assumptions, ms=_ms(t0))


def rule_based_parse(query: str, now: datetime, why: str) -> ParseResult:
    """The fallback parse on its own (e.g. when the SLM parse timed out)."""
    return _fallback(query, now.astimezone(IST).date(), "", why, time.perf_counter())


def _fallback(query: str, today: date, raw: str, why: str, t0: float) -> ParseResult:
    """Activity from synonyms and the detected language only; with no location the gate then
    asks for one."""
    activity = normalize_activity(query)
    parsed = ParsedQuery(in_scope=activity is not None, activity=activity, lang=detect_lang(query))
    note = f"{why}: rule-based parse (activity and language only)"
    return ParseResult(parsed=parsed, raw=raw, assumptions=[note], ms=_ms(t0), fallback=True)


def normalize(data: dict[str, Any], query: str, today: date) -> tuple[ParsedQuery, list[str]]:
    """Deterministic post-processing of the grammar-valid model output. Words the user
    literally said (day, sun event, radius, condition) beat the model; the model's place names,
    age group and "other" condition are kept only when the query supports them."""
    assumptions: list[str] = []
    out: dict[str, Any] = {k: v for k, v in data.items() if v not in ("", None, [], {})}
    raw = out.get("activity_raw")
    out["activity_raw"] = raw.strip() or None if isinstance(raw, str) else None

    # Activity: synonyms of exactly one activity in the query beat a model pick they do not
    # support ("daudna" -> running); else the model; else synonyms of its raw words / the query.
    # The grammar forces an enum value, so "gym" comes back as a made-up supported activity.
    # Out of scope only for a clearly unsupported activity (or when the model says the query
    # is not about going out); an activity the query does not name gets a clarifying question.
    said = mentioned_activities(query)
    grounded = bool(said) or _said_in(out["activity_raw"] or "", query)
    if not said and lx.UNSUPPORTED_ACTIVITY.search(query):
        out["in_scope"] = False
        assumptions.append("asks about an activity Bahar does not cover")
    elif out["in_scope"] and not grounded:
        out["activity"] = out["activity_raw"] = None  # both made up
        assumptions.append("activity not recognised: asking which one")
    elif out["in_scope"]:
        if len(said) == 1 and out.get("activity") not in said:
            out["activity"] = said.pop().value
        elif out.get("activity") is None:
            guess = normalize_activity(out["activity_raw"] or "") or normalize_activity(query)
            out["activity"] = guess.value if guess else None

    out["location_text"] = _location(out.get("location_text"), query, assumptions)
    native = _native(out.get("location_native"), query) or _native_from_query(query)
    out["location_native"] = native if out["location_text"] else None
    out["commute"] = _commute(out, query, assumptions)
    out["radius_km"] = _radius(out.get("radius_km"), query, assumptions)
    out["time"] = _time(dict(out.get("time", {})), query, today, assumptions)
    out["profile"] = _profile(dict(out.get("profile", {})), query)
    prefs = [p.strip() for p in out.get("preferences", []) if p.strip()]
    out["preferences"] = list(dict.fromkeys(prefs))[:MAX_PREFERENCES]
    out["lang"] = detect_lang(query).value
    return ParsedQuery.model_validate(out), assumptions


async def recover_place(parsed: ParsedQuery, query: str) -> tuple[ParsedQuery, list[str]]:
    """A bare place name ("kal subah Powai walk asthma") leaves the model's location empty.
    Try the query's words that no lexicon claims (adjacent pairs first, capitalised first) with
    the geocoder; accept a result in India whose name agrees with the candidate."""
    if not parsed.in_scope or parsed.location_text or parsed.commute or lx.DEVANAGARI.search(query):
        return parsed, []
    for cand in _place_candidates(query)[:PLACE_RECOVERY_TRIES]:
        place = (await geocode(_with_city(cand, query))).value
        if place is not None and place.name.endswith("India") and agrees(place, cand):
            return (
                parsed.model_copy(update={"location_text": _with_city(cand, query)}),
                [f"read '{cand}' as the place"],
            )
    return parsed, []


def _place_candidates(query: str) -> list[str]:
    """Runs of 1-2 adjacent words that are not activity, time, health or function words."""
    words = re.findall(r"[^\W\d_]+", query)
    free = [not _claimed(w) for w in words]
    cands: list[tuple[int, int, str]] = []
    for i, w in enumerate(words):
        if free[i]:
            if w.lower() not in lx.GENERIC_PLACE_WORDS:  # "Park" alone is not a place name
                cands.append((int(w[0].isupper()), 1, w))
            if i + 1 < len(words) and free[i + 1]:
                two = f"{w} {words[i + 1]}"
                cands.append((int(w[0].isupper()) + int(words[i + 1][0].isupper()), 2, two))
    cands.sort(key=lambda c: (-c[0], -c[1]))  # stable: query order breaks ties
    return list(dict.fromkeys(c[2] for c in cands))


def _claimed(word: str) -> bool:
    """Is this word an activity / time / health / function word rather than a place?"""
    w = word.lower()
    if len(w) < MIN_PLACE_WORD or w in lx.HINGLISH or w in lx.PLACE_STOP_WORDS:
        return True
    if normalize_activity(w) or _time_word(w):
        return True
    cues = (lx.SUNRISE, lx.SUNSET, lx.MONTH, lx.CHILD_CUE, lx.SENIOR_CUE, lx.OTHER_CONDITION_CUE,
            *lx.CONDITION_PHRASES.values())  # fmt: skip
    return any(c.search(w) for c in cues)


def _location(loc: str | None, query: str, assumptions: list[str]) -> str | None:
    if loc is None:
        return None
    loc = lx.LOCATION_NOISE.sub("", loc.strip()).strip(" ,") or None
    if loc is None:
        return None
    if not _named_in(loc, query):
        assumptions.append(f"ignored location '{loc}': not mentioned in the question")
        return None
    words = _words(loc)
    if len(words) == 1 and normalize_activity(loc) is not None:  # "walk", "trek"
        return None
    company = (lx.CHILD_CUE, lx.SENIOR_CUE)
    if all(w in lx.HINGLISH or any(c.fullmatch(w) for c in company) for w in words):
        return None  # "bacchon", "dost": company, not a place
    return _with_city(loc, query)


def _native(loc: str | None, query: str) -> str | None:
    """The place as the user wrote it in Devanagari, when the model copied it from the query:
    extended to the whole place phrase, glued Marathi suffixes cut. Geocoded before the
    transliteration (OSM has name:hi for many places)."""
    loc = loc.strip(" ,") if loc else None
    if not loc or not lx.DEVANAGARI.search(loc) or (start := query.find(loc)) < 0:
        return None
    words = loc.split()
    for w in re.findall(r"[^\s,।?!.]+", query[start + len(loc) :])[:3]:
        if _glued(words[-1]) or w in lx.NATIVE_STOP_WORDS or not lx.DEVANAGARI.search(w):
            break  # a glued suffix ("तलावाजवळ") ends the place phrase too
        words.append(w)
    for suffix in lx.NATIVE_SUFFIXES:
        if words[-1].endswith(suffix) and len(words[-1]) > len(suffix) + 1:
            words[-1] = words[-1][: -len(suffix)]
            break
    return " ".join(words)


def _native_from_query(query: str) -> str | None:
    """The Devanagari words right before the first locative postposition (or a word with one
    glued on), skipping day / time words: "कल सुबह इंडिया गेट के पास" -> "इंडिया गेट"."""
    if not lx.DEVANAGARI.search(query):
        return None
    words = re.findall(r"[^\s,।?!.]+", query)
    m = lx.NATIVE_LOCATIVE.search(query)
    end = len(re.findall(r"[^\s,।?!.]+", query[: m.start()])) if m else None
    glued = next((i for i, w in enumerate(words) if _glued(w)), None)
    if glued is not None and (end is None or glued < end):
        end = glued + 1
    if not end:
        return None
    place: list[str] = []
    for w in reversed(words[:end]):
        if (len(place) >= lx.NATIVE_MAX_WORDS or w in lx.NATIVE_STOP_WORDS or not lx.DEVANAGARI.search(w)
                or _time_word(w)):  # fmt: skip
            break
        place.insert(0, w)
    return _native(" ".join(place), query) if place else None


def _time_word(word: str) -> bool:
    pats = [lx.TODAY, lx.TOMORROW, lx.DAY_AFTER, *lx.WEEKDAYS.values(), *lx.PART_WORDS.values()]
    return any(p.fullmatch(word) for p in pats)


def _glued(word: str) -> bool:
    return any(word.endswith(sfx) and len(word) > len(sfx) + 1 for sfx in lx.NATIVE_SUFFIXES)


def _with_city(name: str, query: str) -> str:
    """Append the city the query names (once, unambiguous) so the geocoder searches there."""
    cities = {lx.CITIES[m.group().lower()] for m in lx.CITY.finditer(query)}
    if len(cities) != 1:
        return name
    city = cities.pop()
    named = {city.lower(), *(a for a, c in lx.CITIES.items() if c == city)}
    if any(a in name.lower() for a in named):
        return name
    return f"{name}, {city}"


def _commute(out: dict[str, Any], query: str, assumptions: list[str]) -> dict[str, str] | None:
    """Keep the named ends of a commute; drop it when degenerate (from == to) or when the
    activity cannot be a commute."""
    commute = out.get("commute") or {}
    ends = {
        k: _with_city(v.strip(), query)
        for k, v in commute.items()
        if v and v.strip() and _named_in(v, query)
    }
    if not ends or ends.get("from", "").casefold() == ends.get("to", "").casefold():
        return None
    activity = out.get("activity")
    if activity is None and out["in_scope"]:
        out["activity"] = Activity.cycling.value
        assumptions.append("commute without an activity: assumed cycling")
    elif activity is not None and Activity(activity) not in COMMUTE_ACTIVITIES:
        return None
    return ends


def _radius(r: float | None, query: str, assumptions: list[str]) -> float | None:
    if r is None and (m := lx.RADIUS.search(query)):
        r = float(m.group(1) or m.group(2))
    if r is None:
        return None
    clamped = min(max(float(r), MIN_RADIUS_KM), MAX_RADIUS_KM)
    if clamped != r:
        assumptions.append(f"search radius limited to {clamped:g} km")
    return clamped


def _time(t: dict[str, Any], query: str, today: date, assumptions: list[str]) -> dict[str, Any]:
    if (hour := t.get("hour")) is not None and not 0 <= hour <= 23:
        assumptions.append(f"ignored hour {hour}: not a valid hour")
        t.pop("hour")

    if lx.NOW.search(query):
        t |= {"day": None, "n_days": None, "part": None, "hour": None, "around": None, "now": True}
        assumptions.append("'now' read as the current hour and the next 3 hours")
        return t

    said = _said_day(query)
    day = t.get("day")
    model_date_ok = isinstance(day, str) and day.startswith("date:") and lx.MONTH.search(query)
    if said and day not in ("this_weekend", "next_weekend") and not model_date_ok:
        t["day"] = said
    elif said is None and day and not model_date_ok:
        # No day word in the question: a day the model volunteers ("weekday:mon" for "evening
        # football") is a guess, unless the words for a weekend / several days are there.
        supported = (day in ("this_weekend", "next_weekend") and lx.WEEKEND.search(query)) or (
            day == "next_n_days" and lx.MULTI_DAY.search(query))  # fmt: skip
        if not supported:
            assumptions.append(f"ignored day '{day}': the question names no day")
            t["day"], t["n_days"] = None, None
    if (n := t.get("n_days")) is not None:  # n_days only means something with next_n_days
        if t.get("day") in (None, "today", "next_n_days"):
            t["day"], t["n_days"] = "next_n_days", min(max(n, 1), 7)
        else:
            t.pop("n_days")

    day = t.get("day")
    if isinstance(day, str) and day.startswith("date:"):
        fixed = _date_on_or_after(day[5:], today)
        if fixed is None:
            assumptions.append(f"ignored date '{day[5:]}': not a calendar date")
            t.pop("day")
        else:
            t["day"] = f"date:{fixed.isoformat()}"

    parts = [p for p, pat in lx.PART_WORDS.items() if pat.search(query)]
    if len(parts) == 1 and t.get("part") not in lx.PART_COMPATIBLE[parts[0]]:
        t["part"] = parts[0]
    for part, pat in lx.PART_HOUR.items():  # "raat 9 baje" -> 21, "subah 6 baje" -> 6
        if (m := pat.search(query)) and (n := int(m.group("h"))) <= 23:
            pm_max = lx.AFTERNOON_PM_MAX_HOUR if part == "afternoon" else lx.PM_MAX_HOUR
            t["hour"] = n + 12 if part != "morning" and 1 <= n <= pm_max else n
            break

    if lx.AFTER_WORK.search(query) and t.get("hour") is None:
        t["part"] = Part.after_work.value
        assumptions.append("'after work' read as a start between 17:00 and 19:00")

    rise, sets = bool(lx.SUNRISE.search(query)), bool(lx.SUNSET.search(query))
    if rise != sets:
        t["around"] = "sunrise" if rise else "sunset"
    return t


def _said_day(query: str) -> str | None:
    """The day the user named: day-after > a single weekday > today / tomorrow (only one)."""
    if lx.DAY_AFTER.search(query):
        return "day_after"
    weekdays = [d for d, pat in lx.WEEKDAYS.items() if pat.search(query)]
    if len(weekdays) == 1:
        return f"weekday:{weekdays[0]}"
    today, tomorrow = bool(lx.TODAY.search(query)), bool(lx.TOMORROW.search(query))
    if today != tomorrow:
        return "today" if today else "tomorrow"
    return None


def _profile(profile: dict[str, Any], query: str) -> dict[str, Any]:
    said = [c for c, pat in lx.CONDITION_PHRASES.items() if pat.search(query)]
    conditions = list(dict.fromkeys([*profile.get("conditions", []), *said]))
    if Condition.other.value in conditions and (
        len(conditions) > 1 or not lx.OTHER_CONDITION_CUE.search(query)
    ):
        conditions.remove(Condition.other.value)
    profile["conditions"] = conditions
    cue = {"child": lx.CHILD_CUE, "senior": lx.SENIOR_CUE}.get(profile.get("age_group", ""))
    if cue is not None and not cue.search(query):
        profile.pop("age_group")
    if profile.get("age_group") in (None, "adult") and lx.SENIOR_SAID.search(query):
        profile["age_group"] = "senior"
    if (age := _said_age(query)) is not None:
        if age >= lx.SENIOR_AGE:
            profile["age_group"] = "senior"
        elif age <= lx.CHILD_AGE:
            profile["age_group"] = "child"
    size = profile.get("group_size")
    if size is not None and not 1 <= size <= MAX_GROUP_SIZE:
        profile.pop("group_size")
    return profile


def _said_age(query: str) -> int | None:
    """An age the user stated ("65 saal", "मेरी उम्र 65 साल"), not a duration ("2 saal se")."""
    for m in lx.AGE.finditer(query):
        if lx.DURATION_AFTER.match(query[m.end() :]) or lx.DURATION_BEFORE.search(query[: m.start()]):
            continue
        return int(m.group("n") or m.group("m"))  # int() reads Devanagari digits too
    return None


def _date_on_or_after(iso: str, today: date) -> date | None:
    """The model cannot know the year: keep month and day, take the next occurrence."""
    try:
        _, month, day = (int(x) for x in iso.split("-"))
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def _words(text: str) -> list[str]:
    return re.findall(r"[\wऀ-ॿ]+", text.lower())


def _said_in(text: str, query: str) -> bool:
    """Is (a word of) `text` in the query, allowing typos?"""
    q_words = _words(query)
    return any(
        SequenceMatcher(None, w, q).ratio() >= PLACE_MATCH_RATIO
        for w in _words(text)
        if len(w) >= MIN_PLACE_WORD
        for q in q_words
    )


def _named_in(name: str, query: str) -> bool:
    """`_said_in` for place names. Devanagari queries are always trusted: the model
    transliterates their place names to Latin letters."""
    return bool(lx.DEVANAGARI.search(query)) or _said_in(name, query)


def detect_lang(query: str) -> Lang:
    words = set(_words(query))
    if lx.DEVANAGARI.search(query):
        return Lang.mr if words & lx.MARATHI_DEVANAGARI else Lang.hi
    if words & lx.MARATHI_LATIN:
        return Lang.mr
    return Lang.hinglish if words & lx.HINGLISH else Lang.en


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
