"""Answer text: fill {{e#}} / {{v#}} refs with formatted values, footnote each value with its
source, and append the notes, the source list and the attribution line."""

import re
from collections.abc import Mapping

from bahar.config.sources import SOURCE_NAMES
from bahar.llm import templates
from bahar.llm.validator import REF
from bahar.models import Evidence, Lang

_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")

# Decimals shown per unit (values are stored with more precision than is meaningful).
_DECIMALS: dict[str, int] = {
    "%": 0, "µg/m³": 0, "°C": 1, "min": 0, "km": 1, "m": 0, "mm": 1, "mm/h": 1, "km/h": 0,
    "J/kg": 0,
}  # fmt: skip
_TIGHT_UNITS = frozenset({"%"})  # written without a space: "11%"


def format_value(e: Evidence) -> str:
    if isinstance(e.value, str):
        # Times carry unit IST; the answer is about one place in India, so it is left out.
        return e.value if e.unit in (None, "IST") else f"{e.value} {e.unit}"
    if e.unit == "×":
        return f"×{e.value:g}"
    if e.unit is None:
        return f"{e.value:g}"
    n = f"{e.value:.{_DECIMALS.get(e.unit, 1)}f}"
    return f"{n}{e.unit}" if e.unit in _TIGHT_UNITS else f"{n} {e.unit}"


def fill(text: str, evidence: Mapping[str, Evidence]) -> tuple[str, list[str]]:
    """Replace refs; numeric values get a superscript footnote number per source (times, days
    and names stay clean). Returns the text and the source keys in footnote order. A unit the
    writer repeated after a ref is dropped."""
    order: list[str] = []
    out: list[str] = []
    pos = 0
    for m in REF.finditer(text):
        out.append(text[pos : m.start()])
        pos = m.end()
        e = evidence.get(m.group(1))
        if e is None:  # validated text never has one; the template path cannot
            continue
        value = format_value(e)
        if not isinstance(e.value, str):
            if e.source.value not in order:
                order.append(e.source.value)
            value += str(order.index(e.source.value) + 1).translate(_SUPERSCRIPT)
            if e.unit and (dup := re.match(rf"\s?{re.escape(e.unit)}(?!\w)", text[pos:])):
                pos += dup.end()
        out.append(value)
    out.append(text[pos:])
    return "".join(out), order


def render_answer(
    body: str,
    evidence: Mapping[str, Evidence],
    lang: Lang,
    *,
    modelled: bool,
    disclaimer: bool,
    attribution: str,
) -> str:
    text, order = fill(body, evidence)
    notes = []
    if modelled:
        notes.append(templates.MODELLED_NOTE[lang])
    if disclaimer:
        notes.append(templates.DISCLAIMER[lang])
    footer = [f"_{n}_" for n in notes]
    if order:
        refs = " · ".join(
            f"{str(i + 1).translate(_SUPERSCRIPT)} {SOURCE_NAMES[k]}" for i, k in enumerate(order)
        )
        footer.append(f"{templates.SOURCES_LABEL[lang]}: {refs}")
    footer.append(attribution)
    return text.strip() + "\n\n" + "\n".join(footer)
