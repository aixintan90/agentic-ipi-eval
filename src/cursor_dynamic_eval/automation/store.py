from __future__ import annotations

import hashlib
import json
import random
import secrets
import sqlite3
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .corpus import ExperimentCase


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"b{stamp}-{secrets.token_hex(3)}"


def _case_payload(case: ExperimentCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "chain_id": case.chain_id,
        "user_prompt": case.user_prompt,
        "tool_response_on": case.tool_response_on,
        "tool_response_off": case.tool_response_off,
        "prompt_condition": case.prompt_condition,
        "target_template": case.target_template,
        "metadata": case.metadata,
    }


class BatchStore:
    """Persistent queue. A process crash leaves items recoverable on the next run."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS automation_batches (
                    batch_id TEXT PRIMARY KEY,
                    corpus_path TEXT NOT NULL,
                    corpus_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_items (
                    item_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    chain_id TEXT NOT NULL,
                    injection TEXT NOT NULL,
                    repetition INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT,
                    error TEXT,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(batch_id) REFERENCES automation_batches(batch_id),
                    UNIQUE(batch_id, case_id, injection, repetition)
                );
                CREATE INDEX IF NOT EXISTS idx_automation_queue
                    ON automation_items(batch_id, status, ordinal);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(automation_batches)"
                ).fetchall()
            }
            if "last_error" not in columns:
                connection.execute(
                    "ALTER TABLE automation_batches ADD COLUMN last_error TEXT"
                )

    def create_batch(
        self,
        *,
        corpus_path: Path,
        cases: Iterable[ExperimentCase],
        injections: tuple[str, ...],
        repetitions: int,
        config: dict[str, Any],
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        if repetitions < 1:
            raise ValueError("repetitions must be at least 1")
        if not injections or set(injections) - {"on", "off"}:
            raise ValueError("injections must contain on and/or off")
        resolved = corpus_path.resolve()
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        actual_batch_id = batch_id or new_batch_id()
        stamp = _now()
        rows: list[tuple[object, ...]] = []
        ordinal = 0
        for case in cases:
            for injection in injections:
                if injection not in case.available_injections():
                    raise ValueError(
                        f"case {case.case_id} has no {injection} Tool Response"
                    )
                for repetition in range(1, repetitions + 1):
                    ordinal += 1
                    identity = f"{actual_batch_id}:{case.case_id}:{injection}:{repetition}"
                    item_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                    rows.append(
                        (
                            item_id,
                            actual_batch_id,
                            case.case_id,
                            case.chain_id,
                            injection,
                            repetition,
                            ordinal,
                            "pending",
                            json.dumps(_case_payload(case), ensure_ascii=False, sort_keys=True),
                            stamp,
                        )
                    )
        if config.get("randomize_order"):
            seed = str(config.get("randomization_seed") or actual_batch_id)
            random.Random(seed).shuffle(rows)
            rows = [
                (*row[:6], index, *row[7:])
                for index, row in enumerate(rows, start=1)
            ]
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_batches(
                    batch_id, corpus_path, corpus_sha256, status,
                    config_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'ready', ?, ?, ?)
                """,
                (
                    actual_batch_id,
                    str(resolved),
                    digest,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    stamp,
                    stamp,
                ),
            )
            connection.executemany(
                """
                INSERT INTO automation_items(
                    item_id, batch_id, case_id, chain_id, injection,
                    repetition, ordinal, status, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return self.summary(actual_batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown batch_id: {batch_id}")
        value = dict(row)
        value["config"] = json.loads(value.pop("config_json"))
        return value

    def list_batches(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT batch_id FROM automation_batches ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self.summary(str(row["batch_id"])) for row in rows]

    def summary(self, batch_id: str) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM automation_items WHERE batch_id = ? GROUP BY status
                """,
                (batch_id,),
            ).fetchall()
        counts = Counter({str(row["status"]): int(row["count"]) for row in rows})
        total = sum(counts.values())
        completed = counts["evaluated"] + counts["failed"]
        return {
            **batch,
            "counts": dict(counts),
            "total": total,
            "completed": completed,
            "remaining": total - completed,
        }

    def set_batch_status(self, batch_id: str, status: str) -> None:
        if status not in {"ready", "running", "paused", "completed", "stopped"}:
            raise ValueError(f"invalid batch status: {status}")
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE automation_batches SET status = ?, updated_at = ? WHERE batch_id = ?",
                (status, _now(), batch_id),
            )
        if not result.rowcount:
            raise ValueError(f"unknown batch_id: {batch_id}")

    def set_batch_error(self, batch_id: str, error: str | None) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE automation_batches
                SET last_error = ?, updated_at = ? WHERE batch_id = ?
                """,
                (error[:4000] if error else None, _now(), batch_id),
            )
        if not result.rowcount:
            raise ValueError(f"unknown batch_id: {batch_id}")

    def recover_interrupted(self, batch_id: str) -> int:
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE automation_items
                SET status = 'retry_pending', error = 'worker interrupted', updated_at = ?
                WHERE batch_id = ? AND status = 'running'
                """,
                (_now(), batch_id),
            )
        return int(result.rowcount)

    def claim_next(self, batch_id: str, *, max_attempts: int) -> dict[str, Any] | None:
        stamp = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM automation_items
                WHERE batch_id = ?
                  AND status IN ('pending', 'retry_pending')
                  AND attempts < ?
                ORDER BY ordinal LIMIT 1
                """,
                (batch_id, max_attempts),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE automation_items
                SET status = 'running', attempts = attempts + 1,
                    started_at = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (stamp, stamp, row["item_id"]),
            )
        value = dict(row)
        value["attempts"] = int(value["attempts"]) + 1
        value["status"] = "running"
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def bind_run(self, item_id: str, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE automation_items SET run_id = ?, updated_at = ? WHERE item_id = ?",
                (run_id, _now(), item_id),
            )

    def finish(self, item_id: str, result: dict[str, Any]) -> None:
        stamp = _now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE automation_items
                SET status = 'evaluated', result_json = ?, error = NULL,
                    finished_at = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (json.dumps(result, ensure_ascii=False, sort_keys=True), stamp, stamp, item_id),
            )

    def fail(self, item_id: str, error: str, *, retry: bool) -> None:
        stamp = _now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE automation_items
                SET status = ?, error = ?, finished_at = ?, updated_at = ?
                WHERE item_id = ?
                """,
                ("retry_pending" if retry else "failed", error[:4000], stamp, stamp, item_id),
            )

    def release_blocked(self, item_id: str, error: str) -> None:
        """Return an unstarted item to the queue without consuming an attempt."""
        stamp = _now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE automation_items
                SET status = 'pending', attempts = MAX(0, attempts - 1),
                    run_id = NULL, error = ?, started_at = NULL,
                    finished_at = NULL, updated_at = ?
                WHERE item_id = ? AND status = 'running'
                """,
                (error[:4000], stamp, item_id),
            )

    def recent_items(self, batch_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_items WHERE batch_id = ?
                ORDER BY ordinal DESC LIMIT ?
                """,
                (batch_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            if value.get("result_json"):
                value["result"] = json.loads(value.pop("result_json"))
            else:
                value.pop("result_json", None)
            result.append(value)
        return result

    def paged_items(
        self,
        batch_id: str,
        *,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        actual_page = max(1, int(page))
        actual_page_size = min(100, max(1, int(page_size)))
        with self.connect() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM automation_items WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()["count"]
            )
            pages = max(1, (total + actual_page_size - 1) // actual_page_size)
            actual_page = min(actual_page, pages)
            offset = (actual_page - 1) * actual_page_size
            rows = connection.execute(
                """
                SELECT * FROM automation_items WHERE batch_id = ?
                ORDER BY ordinal LIMIT ? OFFSET ?
                """,
                (batch_id, actual_page_size, offset),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            if value.get("result_json"):
                value["result"] = json.loads(value.pop("result_json"))
            else:
                value.pop("result_json", None)
            result.append(value)
        return {
            "items": result,
            "pagination": {
                "page": actual_page,
                "page_size": actual_page_size,
                "total": total,
                "pages": pages,
            },
        }

    def items(self, batch_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_items WHERE batch_id = ?
                ORDER BY ordinal
                """,
                (batch_id,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            if value.get("result_json"):
                value["result"] = json.loads(value.pop("result_json"))
            else:
                value.pop("result_json", None)
            result.append(value)
        return result
