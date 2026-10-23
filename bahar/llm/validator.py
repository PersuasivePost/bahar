"""Grounding validator for composer output (CLAUDE.md "Grounding contract").

The composer grammar already makes digits and unknown refs impossible; this re-checks both
(the grammar is a decoding aid, the validator is the contract) and adds what a grammar
cannot express: number words next to a unit or time word, place names written outside a ref,
a missing recommended place / time, the wrong script, and refs used in the wrong place: each
labelled line may only cite its own section's facts, and a range {{a}}–{{b}} must be two times.
Qualitative judgements must be grounded too: no clean-air words in a line citing air worse
than "satisfactory", and a heat word must be followed by a temperature / heat-level / time
ref, never another kind of value ("garmi {{e3}}" with e3 in µg/m³).
"""

import re
from collections.abc import Collection, Iterable, Mapping

from bahar.config import lexicon as lx
from bahar.models import Lang

REF = re.compile(r"\{\{\s*([ev]\d+)\s*\}\}")
_RANGE = re.compile(r"\{\{\s*([ev]\d+)\s*\}\}\s*[–-]\s*\{\{\s*([ev]\d+)\s*\}\}")
HEAD = ""  # section key of the first line
_WORD = re.compile(r"[\wऀ-ॿ'’]+|[%°]")
_TOKEN = re.compile(r"\{\{\s*[ev]\d+\s*\}\}|[\wऀ-ॿ'’]+")
MIN_ANSWER_CHARS = 40


def refs_in(text: str) -> list[str]:
    return REF.findall(text)


def place_names(names: Iterable[str]) -> list[str]:
    """Names worth checking: at least one non-generic word ("Hiranandani Garden" yes,
    "Running Track" no), longest first so a full name is reported before its parts."""
    out = set()
    for name in names:
        name = name.strip()
        words = [w for w in re.findall(r"[\wऀ-ॿ]+", name.lower()) if len(w) > 2]
        if words and not all(w in lx.GENERIC_PLACE_WORDS for w in words):
            out.add(name)
    return sorted(out, key=len, reverse=True)


def validate(
    text: str,
    allowed: Collection[str],
    names: Iterable[str],
    required: Iterable[str],
    lang: Lang,
    *,
    times: Collection[str] = (),
    air_levels: Mapping[str, str] | None = None,
    heat_refs: Collection[str] | None = None,
) -> list[str]:
    """Problems found (empty = passed). `allowed` = refs present in the facts block,
    `names` = venue / place names from this query's data, `required` = refs that must appear,
    `lang` = the language the answer must be in (checked by script), `times` = refs whose
    value is a clock time, `air_levels` = PM value / category ref -> NAQI category,
    `heat_refs` = refs a heat word may describe (temperatures, heat levels, times). Line
    placement is checked separately (`misplaced`)."""
    problems = []
    if len(text.strip()) < MIN_ANSWER_CHARS:
        return ["answer empty or too short"]
    refs = refs_in(text)
    if unknown := sorted(set(refs) - set(allowed)):
        problems.append(f"unknown refs {', '.join(unknown)}")
    if missing := [r for r in required if r not in refs]:
        problems.append(f"missing refs {', '.join(missing)}")
    if "{{" in REF.sub("", text) or "}}" in REF.sub("", text):
        problems.append("malformed ref")
    bare = REF.sub(" ", text)
    devanagari = bool(lx.DEVANAGARI.search(bare))
    if devanagari != (lang in (Lang.hi, Lang.mr)):
        problems.append(f"wrong script for {lang.value}")
    if digits := sorted({ch for ch in bare if ch.isdigit()}):
        problems.append(f"digit written outside a ref ({''.join(digits)})")
    words = [w.lower() for w in _WORD.findall(bare)]
    for i, w in enumerate(words):
        if w in lx.NUMBER_WORDS and any(
            0 <= j < len(words) and words[j] in lx.UNIT_WORDS for j in (i - 1, i + 1)
        ):
            problems.append(f"number word next to a unit ('{w}')")
            break
    low = bare.casefold()
    for name in place_names(names):
        if re.search(rf"(?<![\w]){re.escape(name.casefold())}(?![\w])", low):
            problems.append(f"place name outside a ref ('{name}')")
            break
    if any(a not in times or b not in times for a, b in _RANGE.findall(text)):
        problems.append("a time range cites values that are not times")
    if air_levels is not None:
        problems += _clean_air_claims(text, air_levels)
    if heat_refs is not None:
        problems += [msg for _, msg in heat_misuse(text, heat_refs)]
    return problems


def _clean_air_claims(text: str, air_levels: Mapping[str, str]) -> list[str]:
    out = []
    for line in text.splitlines():
        worse = [air_levels[r] for r in refs_in(line) if air_levels.get(r, "good") not in lx.CLEAN_AIR_OK]
        if worse and (m := lx.CLEAN_AIR_WORDS.search(REF.sub(" ", line))):
            out.append(f"calls '{worse[0]}' air '{m.group()}'")
    return out


def heat_misuse(text: str, heat_refs: Collection[str]) -> list[tuple[int, str]]:
    """(line index, message) for a heat word followed, on the same line, by a ref that is not
    a temperature, heat level or time."""
    out = []
    for n, line in enumerate(text.splitlines()):
        tokens = _TOKEN.findall(line)
        for i, tok in enumerate(tokens):
            if tok.casefold() not in lx.HEAT_WORDS:
                continue
            ref = next((m for t in tokens[i + 1 : i + 1 + lx.HEAT_REF_WINDOW] if (m := REF.fullmatch(t))), None)
            if ref and ref.group(1) not in heat_refs:
                out.append((n, f"heat word '{tok}' describes {ref.group(1)}, which is not a temperature or heat level"))
                break
    return out


def line_section(line: str, index: int, keys: Collection[str]) -> str | None:
    """Section of an answer line: HEAD for the first, else the label keyword before its colon
    ("**Avoid karein:** ..." -> "avoid karein"); None for an unlabelled line."""
    if index == 0:
        return HEAD
    if ":" not in line:
        return None
    label = REF.sub("", line.split(":", 1)[0]).replace("*", "").strip().casefold()
    return next((k for k in sorted(keys, key=len, reverse=True) if k and k in label), None)  # "why not" first


def misplaced(text: str, sections: Mapping[str, Collection[str]]) -> list[tuple[int, str]]:
    """(line index, message) for lines citing refs from another section of the facts (the
    model followed the few-shot's layout instead of this request's facts)."""
    out = []
    for i, line in enumerate(text.splitlines()):
        key = line_section(line, i, sections)
        if key is not None and key in sections and (stray := [r for r in refs_in(line) if r not in sections[key]]):
            out.append((i, f"line '{key or 'recommendation'}' cites {', '.join(stray)} from another section"))
    return out
