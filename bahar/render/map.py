"""Map page: a per-answer snapshot persisted for 24 h, rendered by GET /map/{id}."""

import re
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from bahar.config.settings import get_settings
from bahar.models import AskResponse, EngineInput, EngineResult, HourCompact, MapSnapshot, MapVenue

MAP_TTL = timedelta(hours=24)
_ID = re.compile(r"[0-9a-f]{12}")  # our own uuid4 hex prefix: safe as a file name

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def map_url(query_id: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/map/{query_id}"


def _dir() -> Path:
    d = Path(get_settings().cache_dir) / "maps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_snapshot(
    inp: EngineInput, res: EngineResult, resp: AskResponse, query: str,
    hourly: list[HourCompact], now: datetime
) -> MapSnapshot:
    assert res.recommendation is not None  # only built for answers with a recommendation
    why = res.recommendation.why_not
    return MapSnapshot(
        query_id=resp.query_id, query=query,
        created_at=now, activity=inp.activity, origin=inp.place, radius_km=inp.radius_km,
        venues=[MapVenue(id=v.id, name=v.name, kind=v.kind, lat=v.lat, lon=v.lon) for v in inp.venues],
        best_venue_id=res.recommendation.best.venue_id,
        why_not_venue_id=why.venue_id if why else None,
        hours=inp.window.starts, slots=res.slots, hourly=hourly, sources=res.sources,
        answer_text=resp.answer_text, evidence=resp.evidence,
        degraded_sources=resp.degraded_sources, attribution=resp.attribution,
    )  # fmt: skip


def save_snapshot(snap: MapSnapshot) -> None:
    d = _dir()
    (d / f"{snap.query_id}.json").write_text(snap.model_dump_json())
    cutoff = datetime.now().timestamp() - MAP_TTL.total_seconds()
    for f in d.glob("*.json"):  # expire lazily on write; load_snapshot also checks age
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def load_snapshot(query_id: str) -> MapSnapshot | None:
    if not _ID.fullmatch(query_id):
        return None
    f = _dir() / f"{query_id}.json"
    if not f.exists() or datetime.now().timestamp() - f.stat().st_mtime > MAP_TTL.total_seconds():
        return None
    return MapSnapshot.model_validate_json(f.read_text())


def render_map(snap: MapSnapshot) -> str:
    return _env.get_template("map.html").render(
        snap=snap.model_dump(mode="json"), title=f"Bahar — {snap.activity.value} near {snap.origin.name.split(',')[0]}"
    )
