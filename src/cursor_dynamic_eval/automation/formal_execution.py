"""Shared service admission control for the formal CLI path (including retries)."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .cli_runner import AdaptiveConcurrency, invoke_cursor_cli


class BatchPaused(RuntimeError):
    """Admission stopped; an unstarted case has no experimental outcome."""


class FormalExecutionGate:
    def __init__(
        self,
        workers: int,
        *,
        stop_path: Path,
        execute: Callable = invoke_cursor_cli,
    ) -> None:
        self.concurrency = AdaptiveConcurrency(workers)
        self.stop_path = stop_path
        self.execute = execute
        self._lock = threading.Lock()
        self.stop_reason: str | None = None
        self.cli_attempts = 0

    def check(self) -> None:
        if self.stop_path.exists():
            raise BatchPaused(f"batch paused by {self.stop_path.name}")
        if self.stop_reason:
            raise BatchPaused(self.stop_reason)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self.concurrency.snapshot(),
                "cli_attempts_this_session": self.cli_attempts,
                "stop_reason": self.stop_reason,
                "operator_stop": self.stop_path.exists(),
            }

    def trip(self, reason: str) -> None:
        with self._lock:
            self.stop_reason = reason

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.check()
        self.concurrency.acquire()
        try:
            self.check()
            result = self.execute(**kwargs)
            with self._lock:
                self.cli_attempts += 1
            if result.get("outcome") not in {"completed", "blocked", "approval_pending"}:
                stream = result.get("stream")
                details = str(result.get("stderr", "")) + json.dumps(
                    getattr(stream, "events", []), ensure_ascii=False
                )
                if re.search(
                    r"resource.?exhausted|rate.?limit|too many requests|\b429\b",
                    details,
                    re.I,
                ):
                    self.concurrency.reduce_after_transient_failure()
                billing_or_auth = (
                    r"unpaid[\s_]?invoice|usage[\s_]?limit|"
                    r"hit.{0,30}(?:limit|quota)|not logged in|"
                    r"unauthenticated|ActionRequiredError"
                )
                if re.search(billing_or_auth, details, re.I):
                    with self._lock:
                        self.stop_reason = (
                            "billing/authentication service failure; "
                            "no new CLI calls will start"
                        )
            return result
        finally:
            self.concurrency.release()
