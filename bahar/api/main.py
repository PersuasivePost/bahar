import asyncio
import contextvars
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from bahar import __version__
from bahar.config.settings import get_settings
from bahar.llm.runtime import LlmRuntime
from bahar.models import AskRequest, AskResponse, Grounding, Health, Status

ATTRIBUTION = (
    "© OpenStreetMap contributors · "
    "Weather & air quality: Open-Meteo.com, CAMS (Copernicus)"
)

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


configure_logging()
log = logging.getLogger("bahar.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = LlmRuntime(get_settings())
    app.state.llm = runtime
    await asyncio.to_thread(runtime.load)
    log.info("startup complete: %s", runtime.health.model_dump_json())
    yield


app = FastAPI(title="Bahar", version=__version__, lifespan=lifespan)


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
    )


def stub_response(query: str) -> AskResponse:
    """Step 1 placeholder: a valid AskResponse until the pipeline is wired in."""
    return AskResponse(
        query_id=request_id_var.get(),
        status=Status.needs_clarification,
        answer_text=f"Bahar is not wired up yet (scaffold stub). Received: {query!r}",
        assumptions=["Stub response: no parsing, data fetching or recommendation was done."],
        grounding=Grounding(validator_passed=True, fallback_used=True),
        attribution=ATTRIBUTION,
    )


@app.post("/ask")
async def ask_post(body: AskRequest) -> AskResponse:
    return stub_response(body.query)


@app.get("/ask")
async def ask_get(q: str, debug: bool = False) -> AskResponse:
    return stub_response(q)
