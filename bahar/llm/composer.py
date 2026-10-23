"""SLM composer: facts block -> answer in the user's language, every value as a ref.

Decoding is constrained by a per-request GBNF grammar whose only refs are the ones in the
facts block and whose characters exclude every digit form, so the model cannot write a number
or cite unknown evidence (llm.validator re-checks both and the rest of the contract).
Modes: `full` writes the whole answer; `summary` only the recommendation and why, the other
sections then come from templates. Hindi and Marathi always use `summary` (token cost; see
llm.prompts).
"""

from typing import Final, Literal

from bahar.llm import templates
from bahar.llm.prompts import (
    LABELS,
    SHORT_LANGS,
    composer_prefix,
    composer_prefix_name,
    composer_suffix,
)
from bahar.llm.runtime import Completion, LlmRuntime
from bahar.llm.validator import HEAD, refs_in
from bahar.models import FactItem, FactSheet, Lang, SlotFacts

Mode = Literal["full", "summary"]

MAX_TOKENS: Final[dict[Mode, int]] = {"full": 200, "summary": 90}
# Two Devanagari lines need ~120-150 tokens (Devanagari costs ~2x the tokens of Hinglish).
SHORT_LANG_MAX_TOKENS: Final = 160
# ASCII, Devanagari, Arabic-Indic, superscript, subscript and full-width digits; braces only
# inside refs.
_NO_DIGITS: Final = r"[^0-9{}०-९٠-٩²³¹⁰-⁹₀-₉０-９]"


def register_composer(runtime: LlmRuntime) -> int:
    """Evaluate and cache the four per-language composer prefixes. Returns their token count."""
    return sum(runtime.register_prefix(composer_prefix_name(lg), composer_prefix(lg)) for lg in Lang)


def available(runtime: LlmRuntime, lang: Lang) -> bool:
    return runtime.has_prefix(composer_prefix_name(lang))


def mode_for(lang: Lang, full_time: bool) -> Mode:
    return "full" if full_time and lang not in SHORT_LANGS else "summary"


def grammar(refs: list[str]) -> str:
    alts = " | ".join(f'"{r}"' for r in dict.fromkeys(refs))
    return (
        "root ::= piece+\n"
        "piece ::= ref | char\n"
        f'ref ::= "{{{{" ({alts}) "}}}}"\n'
        f"char ::= {_NO_DIGITS}\n"
    )


def _tidy(c: Completion) -> str:
    """Drop a line cut off by the token cap or the deadline; one line per section."""
    lines = [ln.strip() for ln in c.text.strip().splitlines() if ln.strip()]
    if c.finish != "stop" and len(lines) > 1:
        lines = lines[:-1]
    return "\n".join(lines)


async def compose(
    runtime: LlmRuntime,
    sheet: FactSheet,
    lang: Lang,
    mode: Mode,
    deadline: float,
    problems: list[str] | None = None,
) -> tuple[str, Completion]:
    """Returns (tidied text, raw completion). `problems` = the validator's reasons for a retry."""
    facts = templates.facts_block(sheet)
    # Short-language prefixes already write two lines; the note is for full-format prefixes.
    summary = mode == "summary" and lang not in SHORT_LANGS
    suffix = composer_suffix(facts, lang, summary=summary, problems=problems)
    cap = SHORT_LANG_MAX_TOKENS if lang in SHORT_LANGS else MAX_TOKENS[mode]
    c = await runtime.generate(composer_prefix_name(lang), suffix, cap, grammar(refs_in(facts)), deadline)
    return _tidy(c), c


def facts_refs(sheet: FactSheet) -> list[str]:
    return refs_in(templates.facts_block(sheet))


def _slot_refs(s: SlotFacts | None) -> set[str]:
    return {s.venue, s.when} if s else set()


def _item_refs(items: list[FactItem]) -> set[str]:
    return {r for it in items for r in it.refs.values()}


def section_refs(sheet: FactSheet, lang: Lang) -> dict[str, set[str]]:
    """Label keyword (casefolded, as the composer writes it in `lang`) -> refs that line may
    cite; HEAD = the first (recommendation) line."""
    why, carry, watch, avoid, why_not = (lb.replace("{v}", "").strip().casefold() for lb in LABELS[lang])
    head = _slot_refs(sheet.best) | {sheet.window} | ({sheet.improves_at} if sheet.improves_at else set())
    wn = sheet.why_not
    return {
        HEAD: head,
        why: head | _item_refs(sheet.best.reasons),
        carry: _item_refs(sheet.carry),
        watch: _item_refs(sheet.watch),
        avoid: _item_refs(sheet.avoid),
        why_not: _slot_refs(wn) | _item_refs(wn.reasons) if wn else set(),
    }


def summary_lines(text: str) -> list[str]:
    """The recommendation + why lines of a summary-mode answer (the model may run on)."""
    return text.splitlines()[:2]
