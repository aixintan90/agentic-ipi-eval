from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..core.events import Event, append_jsonl
from ..paths import RUNTIME_DIR


def event_log_path() -> Path:
    configured = os.environ.get("CURSOR_EVAL_EVENT_LOG")
    return Path(configured).resolve() if configured else RUNTIME_DIR / "mcp_events.jsonl"


def emit(event: Event) -> None:
    append_jsonl(event_log_path(), event)
    print(
        "[eval-event]" + json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )


def events_for_run(run_id: str) -> list[dict[str, object]]:
    path = event_log_path()
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if value.get("run_id") == run_id:
                events.append(value)
    return events
