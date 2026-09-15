"""Persist candidate batches before execution; recover legacy executed prefixes."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .formal_cli import _call_generator


@contextmanager
def exclusive_batch(path: Path):
    """OS-owned lock: released on process death, unlike a stale PID sentinel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def read_raw_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    # Fail loudly on damaged evidence; never silently discard a partial record.
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class CandidateJournal:
    def __init__(self, generator: Any, root: Path, records: list[dict[str, Any]]) -> None:
        self.generator = generator
        self.root = root
        self.known: dict[tuple[str, int], dict[int, dict[str, Any]]] = {}
        for row in records:
            key = (row["case_id"], int(row["round"]))
            ordinal = int(row["round_ordinal"])
            candidate = {
                "candidate_id": row["candidate_id"],
                "prompt": row["user_prompt"],
                "p_type": row["p_type"],
            }
            previous = self.known.setdefault(key, {}).setdefault(ordinal, candidate)
            if previous != candidate:
                raise ValueError(f"conflicting historical candidate: {key}/{ordinal}")

    def __call__(
        self,
        case: Any,
        round_no: int,
        budget: int,
        *,
        prior_attempts: Any,
    ) -> list[dict[str, Any]]:
        identity = hashlib.sha256(case.case_id.encode()).hexdigest()[:20]
        path = self.root / f"{identity}-r{round_no}.json"
        known = self.known.get((case.case_id, round_no), {})
        if path.is_file():
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            if checkpoint["case_id"] != case.case_id or checkpoint["budget"] != budget:
                raise ValueError("candidate checkpoint identity/budget mismatch")
            candidates = checkpoint["candidates"]
        else:
            if any(ordinal < 1 or ordinal > budget for ordinal in known):
                raise ValueError("historical candidate outside frozen budget")
            missing = [ordinal for ordinal in range(1, budget + 1) if ordinal not in known]
            generated = (
                _call_generator(self.generator, case, round_no, budget, prior_attempts)
                if missing else []
            )
            candidates = [
                known.get(ordinal) or generated[ordinal - 1]
                for ordinal in range(1, budget + 1)
            ]
            checkpoint = {
                "schema_version": "sp27-candidate-checkpoint/1.0",
                "case_id": case.case_id,
                "round": round_no,
                "budget": budget,
                "recovered_executed_slots": sorted(known),
                "regenerated_unrecorded_slots": missing if known else [],
                "candidates": candidates,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        if len(candidates) != budget:
            raise ValueError("candidate checkpoint length mismatch")
        for ordinal, expected in known.items():
            if any(candidates[ordinal - 1].get(key) != value for key, value in expected.items()):
                raise ValueError("candidate checkpoint conflicts with raw execution evidence")
        return candidates
