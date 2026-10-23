"""Orchestration of /ask: parse -> gate -> fetch (parallel where the data allows) -> engine
-> facts -> compose -> validate (one retry) -> template fallback -> render. Every stage has a
timeout and a latency entry; nothing raises to the client."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from bahar.config import budget
from bahar.config.activities import ACTIVITIES, normalize_activity
from bahar.config.sources import ATTRIBUTION, IST, MAX_RADIUS_KM, SOURCE_NAMES
from bahar.data.airquality import fetch_air_quality
from bahar.data.geocode import agrees, geocode, search_radius_km
from bahar.data import localosm
from bahar.data.overpass import fetch_context, fetch_venues
from bahar.data.result import Fetch
from bahar.data.routing import fetch_travel
from bahar.data.stations import fetch_stations
from bahar.data.weather import fetch_forecast
from bahar.engine.facts import build_facts
from bahar.engine.run import run_engine
from bahar.engine.sun import sun_days
from bahar.engine.timewindow import resolve_window
from bahar.llm import templates
from bahar.llm.composer import available, compose, facts_refs, mode_for, section_refs, summary_lines
from bahar.llm.parser import detect_lang, parse_query, recover_place, rule_based_parse
from bahar.llm.runtime import LlmRuntime
from bahar.llm.validator import heat_misuse, line_section, misplaced, validate
from bahar.models import (
    Activity,
    AskResponse,
    CitedResult,
    DegradedSource,
    EngineInput,
    EngineResult,
    Evidence,
    FactSheet,
    Gate,
    Grounding,
    HourCompact,
    Lang,
    ParsedQuery,
    Place,
    SourceKey,
    StationSet,
    Status,
    Venue,
)
from bahar.render.map import build_snapshot, map_url, save_snapshot
from bahar.render.text import render_answer

log = logging.getLogger(__name__)

WIDEN_STEPS = 2  # radius doublings before "no venues" (capped at MAX_RADIUS_KM)

GatherStatus = Literal[
    "ok", "out_of_scope", "needs_clarification", "no_venues", "venues_failed", "no_forecast"
]


class Gathered(BaseModel):
    status: GatherStatus
    detail: str = ""
    engine_input: EngineInput | None = None
    degraded: list[DegradedSource] = []
    assumptions: list[str] = []
    osm_extract: str | None = None  # label of the local OSM extract when it supplied the geography
    latency_ms: dict[str, int] = {}


def _activity(parsed: ParsedQuery) -> Activity | None:
    if parsed.activity is not None:
        return parsed.activity
    return normalize_activity(parsed.activity_raw) if parsed.activity_raw else None


def gate(parsed: ParsedQuery) -> Gate | None:
    """Stop before fetching: unsupported activity -> out_of_scope; an unrecognised activity, a
    missing location or commute end -> needs_clarification with exactly one question (in that
    order). A missing time is not a gate: the time resolver defaults it and records it."""
    lang = parsed.lang
    if not parsed.in_scope:
        return Gate(status=Status.out_of_scope, reason="unsupported activity",
                    message=templates.out_of_scope(lang))  # fmt: skip
    if _activity(parsed) is None:
        return Gate(status=Status.needs_clarification, reason="activity not recognised",
                    message=templates.ask_activity(lang))  # fmt: skip
    if parsed.commute is not None:
        if parsed.commute.from_ is None:
            return Gate(status=Status.needs_clarification, reason="commute start missing",
                        message=templates.ASK_COMMUTE_FROM[lang])  # fmt: skip
        if parsed.commute.to is None:
            return Gate(status=Status.needs_clarification, reason="commute end missing",
                        message=templates.ASK_COMMUTE_TO[lang])  # fmt: skip
    elif not parsed.location_text:
        return Gate(status=Status.needs_clarification, reason="location missing",
                    message=templates.ASK_LOCATION[lang])  # fmt: skip
    return None


async def gather_inputs(parsed: ParsedQuery, now: datetime) -> Gathered:
    """gate -> geocode -> venues -> (context | forecast | air quality | stations | routing)."""
    latency: dict[str, int] = {}
    degraded: list[DegradedSource] = []
    if (stop := gate(parsed)) is not None:
        return Gathered(status=stop.status.value, detail=stop.message)
    activity = _activity(parsed)
    assert activity is not None  # gate() checked
    location = parsed.commute.from_ if parsed.commute else parsed.location_text
    assert location is not None  # gate() checked

    t0 = time.perf_counter()
    g = await _geocode(parsed, location)
    latency["geocode"] = _ms(t0)
    degraded += g.degraded
    place = g.value
    if place is None:
        return Gathered(status="needs_clarification", detail=f"could not locate {location!r}",
                        degraded=degraded, latency_ms=latency)  # fmt: skip

    profile = ACTIVITIES[activity]
    radius = min(parsed.radius_km, MAX_RADIUS_KM) if parsed.radius_km else search_radius_km(place, profile.radius_km)
    t0 = time.perf_counter()
    vf = await fetch_venues(activity, place.lat, place.lon, radius)
    widened: list[str] = []
    for _ in range(WIDEN_STEPS):  # nothing found (not a failure): look twice as far, up to the cap
        wider = min(radius * 2, MAX_RADIUS_KM)
        if vf.value != [] or wider <= radius:
            break
        widened.append(f"no places within {radius:g} km; widened to {wider:g} km")
        degraded += vf.degraded
        radius = wider
        vf = await fetch_venues(activity, place.lat, place.lon, radius)
    latency["venues"] = _ms(t0)
    degraded += vf.degraded
    if vf.value is None:  # the search itself failed (Overpass and the Nominatim fallback)
        return Gathered(status="venues_failed", detail="venue search failed",
                        degraded=degraded, latency_ms=latency)  # fmt: skip
    if not vf.value:
        return Gathered(status="no_venues", detail=f"no {activity.value} venues within {radius:g} km",
                        degraded=degraded, latency_ms=latency)  # fmt: skip
    venues = vf.value
    assumptions: list[str] = list(widened)
    if vf.stale_days is not None:
        assumptions.append(f"venues from map data cached {vf.stale_days} days ago (live fetch failed)")
    if any(d.source is SourceKey.nominatim and d.stage == "venues" for d in vf.degraded):
        assumptions.append("OpenStreetMap map service was unavailable: venues come from a Nominatim "
                           "text search (fewer places, no opening hours or access details)")  # fmt: skip

    now = now.astimezone(IST)
    days = [now.date() + timedelta(days=k) for k in range(8)]
    sun = sun_days(place.lat, place.lon, days)
    window = resolve_window(
        parsed.time, now, {d: s.sunrise for d, s in sun.items()}, {d: s.sunset for d, s in sun.items()}
    )
    need_until = (window.starts[-1] if window.starts else now) + timedelta(minutes=profile.duration_min)

    t0 = time.perf_counter()
    ctx_f, fc_f, aq_f, st_f, tr_f = await asyncio.gather(
        fetch_context(venues), fetch_forecast(venues), fetch_air_quality(venues, need_until=need_until),
        _stations(venues), fetch_travel((place.lat, place.lon), venues),
    )  # fmt: skip
    latency["parallel_fetch"] = _ms(t0)
    for name, f in (("context", ctx_f), ("forecast", fc_f), ("airquality", aq_f),
                    ("stations", st_f), ("routing", tr_f)):  # fmt: skip
        latency[f"fetch_{name}"] = f.elapsed_ms  # inside parallel_fetch
        degraded += f.degraded
        if f.stale_days is not None and name == "context":
            assumptions.append(f"pollution-source map data cached {f.stale_days} days ago (live fetch failed)")
    if fc_f.value is None:
        return Gathered(status="no_forecast", detail="weather forecast unavailable",
                        degraded=degraded, latency_ms=latency)  # fmt: skip

    inp = EngineInput(
        parsed=parsed, activity=activity, place=place, radius_km=radius, venues=venues,
        window=window, context=ctx_f.value, forecast=fc_f.value, air_quality=aq_f.value,
        stations=st_f.value, travel=tr_f.value, degraded=degraded, now=now,
    )  # fmt: skip
    local = any(e.source is SourceKey.osm_extract for e in vf.evidence)
    return Gathered(status="ok", engine_input=inp, degraded=degraded, assumptions=assumptions,
                    osm_extract=localosm.source_label() if local else None, latency_ms=latency)  # fmt: skip


async def _geocode(parsed: ParsedQuery, location: str) -> Fetch[Place]:
    """The Devanagari name first (OSM has name:hi for many places, and transliteration can
    miss: इंडिया गेट -> "Indi Gate"), kept when its result agrees with the parser's Latin
    spelling; else the Latin spelling. Commutes use the latter."""
    if parsed.location_native and not parsed.commute:
        native = await geocode(parsed.location_native)
        if native.value is not None and agrees(native.value, location):
            note = f"located '{parsed.location_native}' by its Devanagari name"
            return native.model_copy(update={"value": native.value.model_copy(
                update={"assumptions": [note, *native.value.assumptions]})})  # fmt: skip
    return await geocode(location)


async def _stations(venues: list[Venue]) -> Fetch[StationSet]:
    """Ground stations only refine CAMS: a slow feed never holds up the answer."""
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(fetch_stations(venues), budget.STATIONS_TIMEOUT_S)
    except TimeoutError:
        return Fetch(degraded=[DegradedSource(
            source=SourceKey.waqi, stage="stations", reason="timeout",
            detail=f"station feeds (WAQI / OpenAQ) slower than {budget.STATIONS_TIMEOUT_S:g} s",
            impact="no ground-station bias correction: CAMS used as is, wider uncertainty band",
        )], elapsed_ms=_ms(t0))  # fmt: skip


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ---------------------------------------------------------------- /ask


@dataclass
class _Request:
    query: str
    query_id: str
    now: datetime
    debug: bool
    t0: float = field(default_factory=time.perf_counter)
    lang: Lang = Lang.en
    parsed: ParsedQuery | None = None
    latency: dict[str, int] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    degraded: list[DegradedSource] = field(default_factory=list)

    @property
    def deadline(self) -> float:
        return self.t0 + budget.REQUEST_BUDGET_S

    def left_s(self) -> float:
        return self.deadline - time.perf_counter()

    def respond(self, status: Status, answer: str, **fields: object) -> AskResponse:
        self.latency["total"] = _ms(self.t0)
        fields.setdefault("grounding", Grounding(validator_passed=True, fallback_used=False))
        fields.setdefault("attribution", ATTRIBUTION)
        return AskResponse(
            query_id=self.query_id, status=status, answer_text=answer, parsed=self.parsed,
            assumptions=list(dict.fromkeys(self.assumptions)), degraded_sources=self.degraded,
            latency_ms=self.latency, **fields,  # type: ignore[arg-type]
        )  # fmt: skip


async def ask(runtime: LlmRuntime, query: str, now: datetime, *, query_id: str, debug: bool = False) -> AskResponse:
    """The whole /ask. Never raises: an unexpected failure becomes a polite answer."""
    req = _Request(query=query, query_id=query_id, now=now, debug=debug, lang=detect_lang(query))
    try:
        return await _ask(runtime, req)
    except Exception:
        log.exception("ask failed")
        req.degraded.append(DegradedSource(source=SourceKey.engine, stage="pipeline", reason="parse",
                                           detail="unexpected internal error", impact="no answer"))  # fmt: skip
        return req.respond(Status.error, templates.INTERNAL_ERROR[req.lang])


async def _ask(runtime: LlmRuntime, req: _Request) -> AskResponse:
    t = time.perf_counter()
    try:
        pr = await asyncio.wait_for(parse_query(runtime, req.query, req.now), budget.PARSE_TIMEOUT_S)
    except TimeoutError:
        pr = rule_based_parse(req.query, req.now, "parser timed out")
    parsed, recovered = await recover_place(pr.parsed, req.query)
    req.latency["parse"] = _ms(t)
    req.parsed, req.lang = parsed, parsed.lang
    req.assumptions += [*pr.assumptions, *recovered]
    if (stop := gate(parsed)) is not None:
        return req.respond(stop.status, stop.message)

    t = time.perf_counter()
    try:
        g = await asyncio.wait_for(gather_inputs(parsed, req.now), budget.GATHER_TIMEOUT_S)
    except TimeoutError:
        req.latency["gather"] = _ms(t)
        req.degraded.append(DegradedSource(source=SourceKey.engine, stage="gather", reason="timeout",
                                           detail=f"data fetch exceeded {budget.GATHER_TIMEOUT_S:g} s",
                                           impact="no answer"))  # fmt: skip
        return req.respond(Status.error, templates.INTERNAL_ERROR[req.lang])
    req.latency.update(g.latency_ms)
    req.degraded += g.degraded
    req.assumptions += g.assumptions
    activity = _activity(parsed)
    if g.status == "needs_clarification":
        return req.respond(Status.needs_clarification, templates.PLACE_NOT_FOUND[req.lang])
    if g.status == "venues_failed":
        return req.respond(Status.error, templates.MAP_DATA_BUSY[req.lang])
    if g.status == "no_venues":
        name = activity.value if activity else ""
        return req.respond(Status.no_venues, templates.NO_VENUES[req.lang].format(activity=name))
    if g.status != "ok" or g.engine_input is None:
        return req.respond(Status.error, templates.NO_FORECAST[req.lang])
    inp = g.engine_input

    t = time.perf_counter()
    res = await asyncio.wait_for(asyncio.to_thread(run_engine, inp), budget.ENGINE_TIMEOUT_S)
    req.latency["engine"] = _ms(t)
    req.assumptions += res.assumptions
    if res.recommendation is None:
        return req.respond(Status.error, templates.NO_FORECAST[req.lang])

    t = time.perf_counter()
    cited = build_facts(inp, res, include_hourly_ids=req.debug)
    req.latency["facts"] = _ms(t)
    body, grounding = await _answer(runtime, req, cited, _place_names(inp))

    t = time.perf_counter()
    evidence = {e.id: e for e in cited.evidence}
    attribution = _attribution(inp, g.osm_extract)
    answer = render_answer(body, evidence, req.lang, modelled=cited.facts.modelled_aq,
                           disclaimer=cited.facts.disclaimer, attribution=attribution)  # fmt: skip
    req.latency["render"] = _ms(t)
    rec = cited.recommendation
    resp = req.respond(
        Status(rec.status), answer, recommendation=rec,
        alternatives=[s for s in (rec.alt_venue, rec.alt_time) if s], why_not=rec.why_not,
        advice=cited.advice, hourly=cited.hourly if req.debug else _compact(res),
        evidence=cited.evidence, grounding=grounding, sources=_sources(inp, cited.evidence, g.osm_extract),
        attribution=attribution, map_url=map_url(req.query_id),
    )  # fmt: skip
    try:  # the map is a bonus: never lose the answer over it
        snap = build_snapshot(inp, res, resp, req.query, _compact(res), req.now)
        await asyncio.to_thread(save_snapshot, snap)
    except Exception:
        log.exception("map snapshot failed")
        resp.map_url = None
    return resp


async def _answer(runtime: LlmRuntime, req: _Request, cited: CitedResult, names: list[str]) -> tuple[str, Grounding]:
    """Answer body with refs: the SLM (full or summary mode by time left), validated, one retry
    if time allows, else the deterministic template."""
    sheet = cited.facts
    tpl_lang = templates.TEMPLATE_LANG[req.lang]
    rejections: list[str] = []
    retries = 0

    allowed, required = facts_refs(sheet), [sheet.best.venue, sheet.best.when]
    times = {e.id for e in cited.evidence if e.unit == "IST"}
    grounded = {"times": times, "air_levels": _air_levels(sheet),
                "heat_refs": {e.id for e in cited.evidence
                              if e.unit in ("°C", "IST") or e.label.startswith("heat-stress level")}}  # fmt: skip
    sections = section_refs(sheet, req.lang)
    fallback = False

    def template() -> tuple[str, Grounding]:
        if tpl_lang is not req.lang:
            req.assumptions.append(f"template answer written in {tpl_lang.value}: no {req.lang.value} template")
        body = "  \n".join(templates.answer_lines(sheet, tpl_lang))
        passed = not validate(body, allowed, names, required, tpl_lang, **grounded)  # checked anyway
        return body, Grounding(validator_passed=passed, fallback_used=True, retries=retries,
                               mode="template", rejections=rejections)  # fmt: skip

    if not available(runtime, req.lang) or req.left_s() < budget.COMPOSER_MIN_S:
        rejections.append("composer skipped: model unavailable or no time left")
        return template()
    mode = mode_for(req.lang, req.left_s() >= budget.FULL_COMPOSER_MIN_S)
    problems: list[str] | None = None
    while True:
        t = time.perf_counter()
        try:
            text, c = await compose(runtime, sheet, req.lang, mode, req.deadline, problems)
        except Exception:
            log.exception("composer failed")
            rejections.append("composer error")
            return template()
        req.latency["compose" if retries == 0 else "compose_retry"] = _ms(t)
        log.info("composer %s: %d tokens in %d ms (%s)", mode, c.tokens, c.ms, c.finish)
        # Heat-word misuse is line-local, so it is repaired from the template line like a
        # misplaced line instead of triggering a recompose.
        problems = validate(text, allowed, names, required, req.lang, **{**grounded, "heat_refs": None})
        bad = sorted(misplaced(text, sections) + heat_misuse(text, grounded["heat_refs"]))
        if not problems and not bad:
            break
        rejections.append("; ".join([*problems, *(m for _, m in bad)]))
        log.info("composer output rejected: %s; text=%r", rejections[-1], text)
        if not problems and tpl_lang is req.lang and bad[0][0] > 0:
            # Only some section lines cite the wrong facts: swap in those template lines.
            text = _repair(text, [i for i, _ in bad], templates.answer_lines(sheet, tpl_lang), sections)
            if validate(text, allowed, names, required, req.lang, **grounded) or misplaced(text, sections):
                return template()
            fallback = True
            break
        if retries == 0 and req.left_s() >= budget.RETRY_MIN_S:
            retries, problems = 1, [*problems, *(m for _, m in bad)]
            continue
        return template()
    lines = text.splitlines()
    if mode == "summary":
        head = summary_lines(text)
        if len(head) < 2:  # cut off before the reasons: take them from the template
            head += templates.head_lines(sheet, tpl_lang)[1:]
        lines = head + templates.tail_lines(sheet, tpl_lang)
        if tpl_lang is not req.lang:
            req.assumptions.append(f"{req.lang.value} answer: the SLM wrote the recommendation; lists are "
                                   f"from the {tpl_lang.value} template")  # fmt: skip
    return "  \n".join(lines), Grounding(validator_passed=True, fallback_used=fallback, retries=retries,
                                         mode=mode, rejections=rejections)  # fmt: skip


def _repair(text: str, bad: list[int], template_lines: list[str], sections: dict[str, set[str]]) -> str:
    """Replace each bad answer line with the template line of the same section (dropped when
    the template has none). Template lines cite only their own section's refs."""
    by_section = {line_section(t, i, sections): t for i, t in enumerate(template_lines)}
    out = []
    for i, line in enumerate(text.splitlines()):
        if i in bad:
            line = by_section.get(line_section(line, i, sections), "")
        if line:
            out.append(line)
    return "\n".join(out)


def _air_levels(sheet: FactSheet) -> dict[str, str]:
    """PM value ref and its category ref -> NAQI category, for the clean-air check."""
    slots = [sheet.best, sheet.alt_venue, sheet.alt_time, sheet.why_not]
    out = {}
    for it in (it for s in slots if s for it in s.reasons):
        if it.code == "air_quality" and it.word:
            out |= {it.refs[k]: it.word for k in ("value", "category") if k in it.refs}
    return out


def _place_names(inp: EngineInput) -> list[str]:
    """Names the answer must only write as refs: venues, and the user's place."""
    names = [v.name for v in inp.venues]
    names.append(inp.place.name.split(",")[0])
    for text in (inp.parsed.location_text, inp.parsed.location_native):
        names += text.split(",") if text else []
    return names


def _compact(res: EngineResult) -> list[HourCompact]:
    score = {(s.venue_id, s.start): s.score for s in res.slots}
    return [
        HourCompact(
            venue_id=h.venue_id, t=h.t, score=score.get((h.venue_id, h.t)), pm25_local=h.pm25_local,
            naqi=h.naqi, wbgt=h.wbgt, rain_prob=h.rain_prob, exposure=h.exposure,
            wind_kmh=h.wind_kmh, wind_dir_deg=h.wind_dir_deg,
        )
        for h in res.hourly
    ]


def _attribution(inp: EngineInput, osm_extract: str | None = None) -> str:
    readings = inp.stations.readings if inp.stations else []
    parts = [ATTRIBUTION]
    if osm_extract:
        parts.append(f"Map data: {osm_extract}")
    for src, name in (("waqi", "WAQI (aqicn.org)"), ("openaq", "OpenAQ")):
        providers = sorted({r.provider for r in readings if r.source == src and r.provider})
        if any(r.source == src for r in readings):
            parts.append(f"Ground stations: {name}" + (f" — {', '.join(providers)}" if providers else ""))
    return " · ".join(parts)


def _sources(inp: EngineInput, evidence: list[Evidence], osm_extract: str | None = None) -> list[str]:
    used = {e.source.value for e in evidence} | {"nominatim", "open_meteo_forecast"}
    used.add("osm_extract" if osm_extract else "overpass")
    if inp.air_quality:
        used.add("open_meteo_air_quality")
    used |= {r.source for r in (inp.stations.readings if inp.stations else [])}
    if inp.travel and not all(t.estimated for t in inp.travel):
        used.add("osrm")
    return [osm_extract if key == "osm_extract" and osm_extract else name
            for key, name in SOURCE_NAMES.items() if key in used]  # fmt: skip
