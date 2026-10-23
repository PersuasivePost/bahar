"""Typed return value shared by every fetcher: a value or a reason it is missing."""

from typing import Generic, TypeVar

from pydantic import BaseModel

from bahar.models import DegradedSource, Evidence

T = TypeVar("T")


class Fetch(BaseModel, Generic[T]):
    """Fetchers never raise: failures land in `degraded`, `value` is None (or partial).

    Evidence ids are provisional ("raw:<source>:<key>"); engine/evidence.py re-assigns the
    final e#/v# ids.
    """

    value: T | None = None
    degraded: list[DegradedSource] = []
    evidence: list[Evidence] = []
    elapsed_ms: int = 0
    cached: bool = False
    stale_days: int | None = None  # served from a cache entry older than its fresh TTL (live fetch failed)
