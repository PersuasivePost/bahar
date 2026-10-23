"""Facts builder: picks what the answer may cite (best slot, alternatives, why-not, top
factors, advice, avoid ranges), adds evidence records for the times it needs, and renumbers
only the cited records (v0 user, v1.. venues, e1.. values, in facts-block order) so the SLM
sees short ordered ids. Every id inside the returned models is rewritten to match. Pure."""

import re
from datetime import datetime
from typing import Any

from bahar.config import weights as w
from bahar.config.health import resolve_health
from bahar.engine.aq import naqi_category
from bahar.engine.evidence import EvidenceStore
from bahar.engine.heat import heat_flag
from bahar.engine.recommend import decisive_gate
from bahar.models import (
    CitedResult,
    EngineInput,
    EngineResult,
    FactItem,
    Factor,
    FactSheet,
    ReasonCode,
    ScoredSlot,
    SlotFacts,
    SourceKey,
)

_ID = re.compile(r"[ev]\d+")
_DIGIT = re.compile(r"\d")
R = ReasonCode

# Placeholder names for a factor's / advice item's value ids (e#, in order; v# ids are the
# venue and are skipped). None = an id the phrase does not use. Keyed by code, or (code, gate)
# where a gate cites differently from the soft factor.
_SLOTS: dict[str | tuple[str, bool], tuple[str | None, ...]] = {
    (R.air_quality, True): ("value", "limit"),
    (R.heat, True): ("value", "limit"),
    R.air_quality: ("value",),
    R.heat: ("value",),
    R.inhaled_dose: ("value",),
    R.travel_time: ("value",),
    R.rush_hour: ("value",),
    R.rain: ("value",),
    R.wind: ("value", "gust"),
    R.darkness: ("sunrise", "sunset"),
    R.uv: ("value",),
    R.wet_ground: ("value",),
    R.green_cooling: ("value",),
    R.golden_hour: ("value",),
    R.cloud_colour: ("value",),
    R.closed: ("value",),
    R.outside_radius: ("value",),
    R.access: ("value",),
    R.profile: ("value",),
    "water": ("value",),
    "electrolytes": ("value",),
    "sunscreen_cap": ("value",),
    "rain_gear": ("value",),
    "light_gear": ("sunset",),
    "grip_shoes": ("value",),
    "wind_shift": ("time", "from", "to"),
    "heat_rising": ("time", "value"),
    "wet_ground": ("value",),
    "rush_hour": ("value",),
    "lit_paths": ("sunset",),
    "ease_off_in_heat": ("value",),
}

_HEAT_BANDS = "WBGT flag: < 23 °C low, 23-28 moderate, 28-30 high, 30-32 very high, >= 32 extreme"
WHY_REASONS = 3  # reasons listed for the best slot
OTHER_REASONS = 1  # for the alternatives and the why-not


def _refs(code: str, gate: bool, ids: list[str], store: EvidenceStore) -> tuple[str, dict[str, str]]:
    """(code, placeholder -> id). Thunderstorm cites (what, time) when forecast, or
    (storm energy, rain chance) for the unstable-air risk, which becomes `storm_unstable`."""
    values = [i for i in ids if i.startswith("e") and i in store]
    if code in (R.thunderstorm, "storm_risk"):
        if len(values) >= 2 and store[values[1]].unit == "IST":
            return code, {"what": values[0], "time": values[1]}
        return "storm_unstable", dict(zip(("cape", "rain"), values, strict=False))
    if code == R.pollution_exposure:  # [exposure, source..., wind from]
        out = {"exposure": values[0]} if values else {}
        if len(values) >= 3:
            out["source"] = values[1]
        return code, out
    names = _SLOTS.get((code, gate)) or _SLOTS.get(code, ())
    return code, {n: i for n, i in zip(names, values, strict=False) if n}


def _factor_item(f: Factor, slot: ScoredSlot, store: EvidenceStore) -> FactItem:
    code, refs = _refs(f.code.value, f.gate, f.evidence_ids, store)
    word = None
    if f.code is R.air_quality and slot.pm25_mean is not None:
        word = naqi_category(slot.pm25_mean, w.NAQI_PM25).value.replace("_", " ")
    elif f.code is R.heat:
        word = heat_flag(slot.wbgt_max).value.replace("_", " ")
    elif f.delta < 0 and f.code is R.green_cooling:
        word = "less"
    elif f.label.startswith("lacks:"):
        word = "lacks"
    elif f.code is R.golden_hour:
        word = "blue" if f.label.startswith("blue") else "golden"
    if f.code in (R.air_quality, R.heat) and word and "value" in refs:
        refs["category"] = _category(store, refs["value"], f.code, word)
    return FactItem(code=code, refs=refs, word=word, label=f.label, gate=f.gate)


def _category(store: EvidenceStore, value_id: str, code: ReasonCode, word: str) -> str:
    """The qualitative word next to a number, as its own record, so the answer can only judge
    air and heat through it: "moderate (indicative)", "high heat stress"."""
    v = store[value_id]
    if code is R.air_quality:
        return store.value(f"NAQI category of the {v.label}", f"{word} (indicative)", None, SourceKey.engine,
                           v.fetched_at, note="CPCB NAQI PM2.5 band; indicative: NAQI is defined on "
                                              "24-h averages; modelled estimate")  # fmt: skip
    return store.value(f"heat-stress level for the {v.label}", f"{word} heat stress", None, SourceKey.engine,
                       v.fetched_at, note=_HEAT_BANDS)  # fmt: skip


def _reasons(slot: ScoredSlot, n: int, store: EvidenceStore) -> list[FactItem]:
    """Gates first (decisive one first), then the factors with the largest effect."""
    gates = sorted(slot.gates, key=lambda g: g is not decisive_gate(slot)) if slot.gates else []
    gated = {g.code for g in gates}  # a gate already says it: skip the same code's penalty
    factors = sorted((f for f in slot.factors if f.code not in gated), key=lambda f: -abs(f.delta))
    return [_factor_item(f, slot, store) for f in [*gates, *factors][:n]]


def _versus(item: FactItem, best: ScoredSlot, store: EvidenceStore) -> FactItem:
    """Add the best slot's value of the same factor (placeholder `best`) for a comparison."""
    mine = next((f for f in best.factors if f.code.value == item.code), None)
    if mine is None or "value" not in item.refs:
        return item
    _, refs = _refs(mine.code.value, False, mine.evidence_ids, store)
    if "value" not in refs or store[refs["value"]].unit != store[item.refs["value"]].unit:
        return item
    return item.model_copy(update={"refs": {**item.refs, "best": refs["value"]}})


def _reason_vs_best(slot: ScoredSlot, best: ScoredSlot, store: EvidenceStore) -> list[FactItem]:
    """The one factor where `slot` falls furthest behind `best` (a gate, if blocked and the
    best is not), with the best's value for comparison."""
    if slot.gates and not best.gates:
        return [_factor_item(decisive_gate(slot), slot, store)]
    gap: dict[str, float] = {}
    for f in slot.factors:
        gap[f.code] = gap.get(f.code, 0.0) + f.delta
    for f in best.factors:
        if f.code in gap:
            gap[f.code] -= f.delta
    if not gap:
        return []
    code = min(gap, key=gap.__getitem__)
    if gap[code] >= 0:  # behind on nothing shared: name its largest penalty
        return _reasons(slot, OTHER_REASONS, store)
    f = min((f for f in slot.factors if f.code == code), key=lambda f: f.delta)
    return [_versus(_factor_item(f, slot, store), best, store)]


class _Builder:
    def __init__(self, inp: EngineInput, res: EngineResult, store: EvidenceStore) -> None:
        self.store, self.now = store, inp.now
        self.venues = {v.id: v for v in inp.venues}
        self.refs = res.venue_refs
        rec = res.recommendation
        assert rec is not None
        self.best_day = rec.best.start.date()

    def span(self, what: str, venue_id: str, start: datetime, end: datetime, *, with_day: bool = False) -> str:
        """One record per time range: the SLM places one ref instead of two."""
        day = f"{start:%a %d %b}, " if with_day or start.date() != self.best_day else ""
        return self.store.value(f"{what} at {self.venues[venue_id].name}", f"{day}{start:%H:%M}–{end:%H:%M}",
                                "IST", SourceKey.engine, self.now)  # fmt: skip

    def slot(self, s: ScoredSlot, reasons: list[FactItem], *, with_day: bool = False) -> SlotFacts:
        v = self.venues[s.venue_id]
        return SlotFacts(
            venue=self.refs[s.venue_id], kind=v.kind.replace("_", " "),
            when=self.span("session", s.venue_id, s.start, s.end, with_day=with_day),
            blocked=s.blocked, reasons=reasons,
        )  # fmt: skip


def build_facts(inp: EngineInput, res: EngineResult, *, include_hourly_ids: bool = False) -> CitedResult:
    """Pure: pick what the answer may cite, add the records for its times, renumber.
    Requires res.recommendation."""
    rec, adv = res.recommendation, res.advice
    assert rec is not None
    store = EvidenceStore.from_records(res.evidence)
    b = _Builder(inp, res, store)

    def item(code: str, label: str, ids: list[str]) -> FactItem:
        code, refs = _refs(code, False, ids, store)
        return FactItem(code=code, refs=refs, label=label)

    carry = []
    for it in adv.carry if adv else []:
        if it.code == "n95":
            mask = store.value("recommended mask type", "N95", None, SourceKey.engine, inp.now,
                               note="a well-fitting N95 / FFP2 respirator")  # fmt: skip
            carry.append(FactItem(code="n95", refs={"mask": mask}, label=it.label))
        else:
            carry.append(item(it.code, it.label, it.evidence_ids))
    watch = [item(it.code, it.label, it.evidence_ids) for it in (adv.watch_for if adv else [])]
    avoid = []
    for r in adv.avoid_when if adv else []:
        refs = {"when": b.span("avoid", r.venue_id or rec.best.venue_id, r.start, r.end)}
        avoid.append(FactItem(code=r.reason.value, refs=refs, label=r.reason.value))

    health, _ = resolve_health(inp.parsed.profile)
    why_not = None
    if rec.why_not:
        wn = rec.why_not
        reason = (FactItem(code="close_call") if wn.close
                  else _versus(_factor_item(wn.decisive, wn.slot, store), rec.best, store))
        why_not = b.slot(wn.slot, [reason])
    sheet = FactSheet(
        lang=inp.parsed.lang, activity=inp.activity,
        profile=[] if health.key == "default" else health.key.split("+"),
        status=rec.status,
        window=store.value("requested time", rec.window_label, None, SourceKey.engine, inp.now),
        best=b.slot(rec.best, _reasons(rec.best, WHY_REASONS, store), with_day=True),
        alt_venue=b.slot(rec.alt_venue, _reason_vs_best(rec.alt_venue, rec.best, store)) if rec.alt_venue else None,
        alt_time=b.slot(rec.alt_time, _reason_vs_best(rec.alt_time, rec.best, store)) if rec.alt_time else None,
        why_not=why_not,
        improves_at=store.value(f"conditions improve from, at {b.venues[rec.best.venue_id].name}",
                                f"{rec.improves_at:%a %H:%M}", "IST", SourceKey.engine, inp.now)
        if rec.improves_at else None,
        carry=carry, watch=watch, avoid=avoid,
        disclaimer=bool(res.notes),
    )  # fmt: skip

    # Cited = the facts block first (so ids read in answer order), then whatever the
    # recommendation and advice show.
    order: list[str] = ["v0", *_ids_in(sheet.model_dump()), *_ids_in(rec.model_dump())]
    if adv:
        order += _ids_in(adv.model_dump())
    hourly = res.hourly if include_hourly_ids else []
    if include_hourly_ids:
        order += [i for hc in hourly for i in hc.top_source_ids]
    mapping = _renumber([i for i in dict.fromkeys(order) if i in store])
    sheet = FactSheet.model_validate(_remap(sheet.model_dump(), mapping))
    sheet.modelled_aq = any(store[o].unit == "µg/m³" for o in mapping)
    return CitedResult(
        facts=sheet,
        recommendation=type(rec).model_validate(_remap(rec.model_dump(), mapping)),
        advice=type(adv).model_validate(_remap(adv.model_dump(), mapping)) if adv else None,
        hourly=[type(hc).model_validate(_remap(hc.model_dump(), mapping)) for hc in hourly],
        evidence=[store[old].model_copy(update={"id": new}) for old, new in mapping.items()],
    )


def _ids_in(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj] if _ID.fullmatch(obj) else []
    if isinstance(obj, dict):
        return [i for v in obj.values() for i in _ids_in(v)]
    if isinstance(obj, list):
        return [i for v in obj for i in _ids_in(v)]
    return []


def _renumber(ids: list[str]) -> dict[str, str]:
    """old -> new: v0 stays, other v# and e# count up from 1 in order of first use."""
    out: dict[str, str] = {}
    n = {"e": 0, "v": 0}
    for old in ids:
        if old == "v0":
            out[old] = "v0"
            continue
        n[old[0]] += 1
        out[old] = f"{old[0]}{n[old[0]]}"
    return out


def _remap(obj: Any, mapping: dict[str, str]) -> Any:
    """Rewrite every evidence id inside a model dump; ids not cited are dropped from lists."""
    if isinstance(obj, str):
        return mapping.get(obj, obj) if _ID.fullmatch(obj) else obj
    if isinstance(obj, dict):
        return {k: _remap(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_remap(v, mapping) for v in obj if not (isinstance(v, str) and _ID.fullmatch(v) and v not in mapping)]
    return obj


def digit_free(text: str) -> bool:
    return not _DIGIT.search(text)
