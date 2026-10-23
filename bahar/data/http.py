"""One shared async httpx client: retry with backoff + jitter, per-host rate limits, mirror
rotation, and every failure converted into a DegradedSource (never raised)."""

import asyncio
import logging
import random
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from bahar import __version__
from bahar.config import sources as cfg
from bahar.config.settings import get_settings
from bahar.models import DegradedSource, SourceKey

Reason = Literal["timeout", "http", "rate_limited", "empty", "parse", "horizon"]

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_host_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_host_next_free: dict[str, float] = {}


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        contact = get_settings().nominatim_email or "unset"
        _client = httpx.AsyncClient(
            headers={
                "User-Agent": f"Bahar/{__version__} (GeoAI Hackathon CSRE IIT Bombay; "
                f"contact: {contact})"
            },
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    if _client is not None and not _client.is_closed:
        await _client.aclose()


async def _throttle(host: str) -> None:
    interval = cfg.HOST_MIN_INTERVAL_S.get(host)
    if interval is None:
        return
    async with _host_locks[host]:
        wait = _host_next_free.get(host, 0.0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _host_next_free[host] = time.monotonic() + interval


def _backoff_s(attempt: int, retry_after: str | None) -> float:
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 5.0)
    return cfg.BACKOFF_BASE_S * 2**attempt + random.uniform(0, cfg.BACKOFF_JITTER_S)


async def request(
    source: SourceKey,
    stage: str,
    impact: str,
    method: str,
    urls: str | Sequence[str],
    *,
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = cfg.DEFAULT_TIMEOUT_S,
    deadline_s: float | None = None,
    reject: Callable[[httpx.Response], str | None] | None = None,
    on_attempt: Callable[[str, bool], None] | None = None,
) -> tuple[httpx.Response | None, DegradedSource | None]:
    """Send a request; attempt i uses urls[i % len(urls)] (mirror rotation).

    `timeout_s` is per attempt, `deadline_s` caps the whole retry loop. `reject` inspects a
    200 response and returns a problem description to make it a retryable failure (Overpass
    reports server-side timeouts inside a 200 body). `on_attempt(url, ok)` is called after every
    attempt (circuit breakers).
    """
    candidates = [urls] if isinstance(urls, str) else list(urls)
    deadline = time.monotonic() + (
        deadline_s if deadline_s is not None else timeout_s * cfg.RETRIES
    )
    reason: Reason = "http"
    detail = "deadline exhausted before first attempt"

    for attempt in range(cfg.RETRIES):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            break
        url = candidates[attempt % len(candidates)]
        host = urlsplit(url).netloc
        retry_after: str | None = None
        await _throttle(host)
        try:
            resp = await get_client().request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=min(timeout_s, max(remaining, 1.0)),
            )
        except httpx.TimeoutException:
            reason, detail = "timeout", f"{host}: timed out after {timeout_s:g}s"
        except httpx.HTTPError as exc:
            reason, detail = "http", f"{host}: {type(exc).__name__}: {exc}"[:200]
        else:
            problem = reject(resp) if reject and resp.status_code == 200 else None
            if resp.status_code == 200 and problem is None:
                if on_attempt:
                    on_attempt(url, True)
                return resp, None
            if problem is not None:
                reason, detail = "http", f"{host}: {problem}"
            else:
                body = resp.text[:300].replace("\n", " ")
                reason = "rate_limited" if resp.status_code == 429 else "http"
                detail = f"{host}: HTTP {resp.status_code} {body}"
                if resp.status_code not in cfg.RETRY_STATUSES:
                    break
                retry_after = resp.headers.get("Retry-After")

        if on_attempt:
            on_attempt(url, False)
        log.warning("%s/%s attempt %d failed: %s", source.value, stage, attempt + 1, detail)
        pause = _backoff_s(attempt, retry_after)
        if attempt + 1 < cfg.RETRIES and deadline - time.monotonic() > pause + 0.5:
            await asyncio.sleep(pause)
        else:
            break

    return None, DegradedSource(
        source=source, stage=stage, reason=reason, detail=detail, impact=impact
    )


async def request_json(
    source: SourceKey,
    stage: str,
    impact: str,
    method: str,
    urls: str | Sequence[str],
    *,
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = cfg.DEFAULT_TIMEOUT_S,
    deadline_s: float | None = None,
    reject: Callable[[httpx.Response], str | None] | None = None,
) -> tuple[Any | None, DegradedSource | None]:
    resp, failure = await request(
        source, stage, impact, method, urls, params=params, data=data, headers=headers,
        timeout_s=timeout_s, deadline_s=deadline_s, reject=reject,
    )  # fmt: skip
    if resp is None:
        return None, failure
    try:
        return resp.json(), None
    except ValueError as exc:
        return None, DegradedSource(
            source=source, stage=stage, reason="parse", detail=f"invalid JSON: {exc}"[:200],
            impact=impact,
        )  # fmt: skip


def degraded(
    source: SourceKey, stage: str, reason: Reason, detail: str, impact: str
) -> DegradedSource:
    return DegradedSource(source=source, stage=stage, reason=reason, detail=detail, impact=impact)

