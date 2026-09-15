"""Run-scoped synthetic protected detail helpers for read-only CLI evals."""

from __future__ import annotations

import hashlib
import re

FIXTURE_PROTOCOL = "derived-auditable-v2"


def synthetic_fixture_value(*, run_id: str, case_id: str, prompt_id: str) -> str:
    material = "\n".join([FIXTURE_PROTOCOL, run_id, case_id, prompt_id])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24].upper()
    return f"SYNTHETIC_DISCLOSURE_{digest}"


def synthetic_fixture_reference(*, run_id: str, role: str = "protected-detail") -> str:
    safe_role = "-".join(filter(None, re.split(r"[^a-z0-9]+", role.casefold())))
    return f"mcp-fixture://{run_id}/{safe_role or 'protected-detail'}"
