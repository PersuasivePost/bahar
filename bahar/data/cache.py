"""TTL cache on diskcache: static geography (7 d) vs live weather/AQ (15 min)."""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, TypeVar

from diskcache import Cache

from bahar.config.settings import get_settings
from bahar.config.sources import CACHE_VERSION, LIVE_TTL_S, STATIC_TTL_S
from bahar.data.result import Fetch

T = TypeVar("T")

STATIC = STATIC_TTL_S
LIVE = LIVE_TTL_S


@lru_cache
def _cache() -> Cache:
    return Cache(get_settings().cache_dir)


def make_key(namespace: str, **parts: Any) -> str:
    """Normalised request key: CACHE_VERSION + sorted JSON of the parts, hashed. Callers round
    coordinates."""
    blob = json.dumps({"v": CACHE_VERSION, **parts}, sort_keys=True, default=str, ensure_ascii=False)
    return f"{namespace}:v{CACHE_VERSION}:{hashlib.sha1(blob.encode()).hexdigest()}"


async def cached(key: str, ttl_s: int, fetch: Callable[[], Awaitable[Fetch[T]]]) -> Fetch[T]:
    """Return the cached Fetch or run `fetch`. Only clean successes are stored."""
    started = time.perf_counter()
    hit: Fetch[T] | None = await asyncio.to_thread(_cache().get, key)
    if hit is not None:
        return hit.model_copy(
            update={"cached": True, "elapsed_ms": int((time.perf_counter() - started) * 1000)}
        )
    result = await fetch()
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    if result.value is not None and not result.degraded:
        await asyncio.to_thread(_cache().set, key, result, expire=ttl_s)
    return result
