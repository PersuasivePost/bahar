import asyncio
import contextvars
import ipaddress
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from bahar import __version__
from bahar.config import budget
from bahar.config.settings import get_settings
from bahar.config.sources import ATTRIBUTION, IST
from bahar.data.http import close_client, get_client
from bahar.data import localosm
from bahar.llm import templates
from bahar.llm.composer import register_composer
from bahar.llm.parser import register_parser
from bahar.llm.parser import detect_lang
from bahar.llm.runtime import LlmRuntime
from bahar.models import (
    AskRequest,
    AskResponse,
    DegradedSource,
    Grounding,
    Health,
    Lang,
    SourceKey,
    Status,
)
from bahar.pipeline import ask
from bahar.render.map import load_snapshot, render_map

RATE_WINDOW_S = 60.0

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").disabled = True  # replaced by the request log below
    # httpx logs full URLs at INFO, including API tokens in query strings (WAQI).
    logging.getLogger("httpx").setLevel(logging.WARNING)


configure_logging()
log = logging.getLogger("bahar.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_client()  # one shared httpx client for every fetcher
    runtime = LlmRuntime(get_settings())
    app.state.llm = runtime
    await asyncio.to_thread(runtime.load)
    if runtime.health.model_loaded:
        for name, register in (("parser", register_parser), ("composer", register_composer)):
            t0 = time.perf_counter()
            n = await asyncio.to_thread(register, runtime)
            log.info("%s prefix: %d tokens in %d ms", name, n, (time.perf_counter() - t0) * 1000)
    log.info("startup complete: %s", runtime.health.model_dump_json())
    try:
        yield
    finally:
        await close_client()


app = FastAPI(title="Bahar", version=__version__, lifespan=lifespan)


_hits: defaultdict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    """Behind the Cloudflare tunnel every request arrives from cloudflared on loopback; the
    visitor's address is in CF-Connecting-IP. That header is trusted only from loopback."""
    host = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("cf-connecting-ip")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    return forwarded.strip() if forwarded and loopback else host


def direct_loopback(request: Request) -> bool:
    """A local caller (battery, curl) with no Cloudflare header; tunnel traffic also arrives from
    loopback but carries CF-Connecting-IP and stays limited."""
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback and "cf-connecting-ip" not in request.headers
    except ValueError:
        return False


def rate_limited(ip: str, limit: int, now: float) -> bool:
    """Sliding window: at most `limit` requests per RATE_WINDOW_S per address."""
    hits = _hits[ip]
    while hits and now - hits[0] > RATE_WINDOW_S:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


@app.middleware("http")
async def rate_limit(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if request.url.path == "/ask" and not direct_loopback(request):
        limit = get_settings().rate_limit_per_min
        if rate_limited(client_ip(request), limit, time.monotonic()):
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited", "message": templates.BUSY.format(n=limit)},
                headers={"Retry-After": str(int(RATE_WINDOW_S))},
            )
    return await call_next(request)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    log.info(
        "%s %s -> %d in %d ms",
        request.method,
        request.url.path,
        response.status_code,
        (time.perf_counter() - t0) * 1000,
    )
    return response


@app.get("/health")
async def health(request: Request) -> Health:
    llm: LlmRuntime = request.app.state.llm
    return Health(
        status="ok" if llm.health.model_loaded else "degraded",
        version=__version__,
        llm=llm.health,
        osm_extract=localosm.source_label(),
    )


def _error_response(query_id: str, query: str, source_detail: str) -> AskResponse:
    lang = detect_lang(query)
    return AskResponse(
        query_id=query_id, status=Status.error, answer_text=templates.INTERNAL_ERROR[lang],
        grounding=Grounding(validator_passed=True, fallback_used=False), attribution=ATTRIBUTION,
        degraded_sources=[DegradedSource(source=SourceKey.engine, stage="api", reason="timeout",
                                         detail=source_detail, impact="no answer")],
    )  # fmt: skip


async def _ask(request: Request, query: str, debug: bool) -> AskResponse:
    # query_id is ours, never the client's X-Request-ID: the map page will use it as a file name.
    query_id = uuid.uuid4().hex[:12]
    try:
        return await asyncio.wait_for(
            ask(request.app.state.llm, query, datetime.now(IST), query_id=query_id, debug=debug),
            budget.REQUEST_HARD_TIMEOUT_S,
        )
    except TimeoutError:
        log.error("ask exceeded %g s", budget.REQUEST_HARD_TIMEOUT_S)
        return _error_response(query_id, query, f"request exceeded {budget.REQUEST_HARD_TIMEOUT_S:g} s")
    except Exception:
        log.exception("ask crashed")
        return _error_response(query_id, query, "unhandled error")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Last resort for every route: JSON instead of a dropped connection."""
    log.error("unhandled error on %s", request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"status": "error", "answer_text": templates.INTERNAL_ERROR[Lang.en]})


@app.post("/ask")
async def ask_post(request: Request, body: AskRequest, debug: bool = False) -> AskResponse:
    return await _ask(request, body.query, debug)


@app.get("/ask")
async def ask_get(
    request: Request, q: Annotated[str, Query(min_length=1, max_length=1000)], debug: bool = False
) -> AskResponse:
    return await _ask(request, q, debug)


@app.get("/map/{query_id}", response_class=HTMLResponse)
async def map_page(query_id: str) -> HTMLResponse:
    snap = await asyncio.to_thread(load_snapshot, query_id)
    if snap is None:
        return HTMLResponse("<p>This map has expired or does not exist (maps are kept for 24 h).</p>", status_code=404)
    return HTMLResponse(render_map(snap))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)
