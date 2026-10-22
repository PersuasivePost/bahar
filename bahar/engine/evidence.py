"""EvidenceStore: assigns the final ids the SLM may cite. e1..eN = values, v1..vN = venue /
place names, v0 = the user's own geocoded location. Identical records share one id."""

from collections.abc import Iterable
from datetime import datetime

from bahar.models import Evidence, SourceKey

_Key = tuple[str, str, float | str, str | None, SourceKey, str | None]


class EvidenceStore:
    def __init__(self) -> None:
        self._records: dict[str, Evidence] = {}
        self._ids: dict[_Key, str] = {}
        self._next_e = 1
        self._next_v = 1

    def _put(self, prefix: str, label: str, value: float | str, unit: str | None,
             source: SourceKey, fetched_at: datetime, note: str | None) -> str:  # fmt: skip
        key: _Key = (prefix, label, value, unit, source, note)
        if key in self._ids:
            return self._ids[key]
        if prefix == "v0":
            eid = "v0"
        elif prefix == "v":
            eid, self._next_v = f"v{self._next_v}", self._next_v + 1
        else:
            eid, self._next_e = f"e{self._next_e}", self._next_e + 1
        self._ids[key] = eid
        self._records[eid] = Evidence(
            id=eid, label=label, value=value, unit=unit, source=source,
            fetched_at=fetched_at, note=note,
        )  # fmt: skip
        return eid

    def value(self, label: str, value: float | str, unit: str | None, source: SourceKey,
              fetched_at: datetime, note: str | None = None) -> str:  # fmt: skip
        return self._put("e", label, value, unit, source, fetched_at, note)

    def place(self, name: str, label: str, source: SourceKey, fetched_at: datetime,
              *, user_location: bool = False) -> str:  # fmt: skip
        return self._put("v0" if user_location else "v", label, name, None, source, fetched_at, None)

    def __getitem__(self, eid: str) -> Evidence:
        return self._records[eid]

    def __len__(self) -> int:
        return len(self._records)

    def records(self, ids: Iterable[str] | None = None) -> list[Evidence]:
        """All records, or the given ids in order (for the response's evidence list)."""
        if ids is None:
            return list(self._records.values())
        return [self._records[i] for i in dict.fromkeys(ids)]
