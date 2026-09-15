from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Event:
    surface: str
    kind: str
    ts: float
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    source: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.surface or not self.kind:
            raise ValueError("surface and kind are required")
        if self.ts <= 0:
            raise ValueError("ts must be a positive unix timestamp")
        if self.event_id is None:
            payload = json.dumps(
                {
                    "run_id": self.run_id,
                    "surface": self.surface,
                    "kind": self.kind,
                    "ts": round(self.ts, 6),
                    "data": self.data,
                    "source": self.source,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            object.__setattr__(
                self,
                "event_id",
                hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
            )

    @classmethod
    def now(
        cls,
        surface: str,
        kind: str,
        *,
        run_id: str | None = None,
        data: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> Event:
        return cls(
            surface=surface,
            kind=kind,
            ts=time.time(),
            run_id=run_id,
            data=data or {},
            source=source,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Event:
        return cls(
            run_id=value.get("run_id"),
            surface=str(value["surface"]),
            kind=str(value["kind"]),
            ts=float(value["ts"]),
            data=dict(value.get("data") or {}),
            source=value.get("source"),
            event_id=value.get("event_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "surface": self.surface,
            "kind": self.kind,
            "ts": self.ts,
            "data": self.data,
            "source": self.source,
        }


def sort_events(events: Iterable[Event]) -> list[Event]:
    return sorted(events, key=lambda item: item.ts)


def append_jsonl(path: Path, event: Event | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = event.to_dict() if isinstance(event, Event) else event
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[Event]:
    if not path.exists():
        return []
    events: list[Event] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
                events.append(Event.from_dict(value))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_no}: invalid event: {exc}") from exc
    return sort_events(events)


def merge_events(*groups: Iterable[Event]) -> list[Event]:
    unique: dict[str, Event] = {}
    for group in groups:
        for event in group:
            unique[event.event_id or ""] = event
    return sort_events(unique.values())
