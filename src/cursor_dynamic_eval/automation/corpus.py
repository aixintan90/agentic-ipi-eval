from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import render_template

CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VALID_CHAINS = {f"CHAIN-{index:02d}" for index in range(1, 10)}
VALID_INJECTIONS = {"on", "off"}


@dataclass(frozen=True)
class ExperimentCase:
    case_id: str
    chain_id: str
    user_prompt: str
    tool_response_on: str
    tool_response_off: str | None = None
    prompt_condition: str = "corpus"
    target_template: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def available_injections(self) -> tuple[str, ...]:
        return ("on", "off") if self.tool_response_off is not None else ("on",)

    def response_for(self, injection: str, variables: dict[str, object]) -> str:
        if injection not in VALID_INJECTIONS:
            raise ValueError(f"invalid injection: {injection}")
        template = self.tool_response_on if injection == "on" else self.tool_response_off
        if template is None:
            raise ValueError(f"case {self.case_id} has no {injection} response")
        expanded = {**variables, "target": str(variables.get("target") or "")}
        return render_template(template, expanded)

    def target_for(self, variables: dict[str, object], fallback: str) -> str:
        if not self.target_template:
            return fallback
        return render_template(self.target_template, variables)


def _pick(value: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return None


def _case_from_dict(value: dict[str, Any], *, index: int) -> ExperimentCase:
    case_id = str(_pick(value, "case_id", "id") or f"case-{index:05d}").strip()
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError(f"invalid case_id at item {index}: {case_id!r}")
    chain_id = str(_pick(value, "chain_id", "chain") or "").upper().strip()
    if chain_id not in VALID_CHAINS:
        raise ValueError(f"case {case_id}: invalid chain_id {chain_id!r}")
    prompt = str(_pick(value, "user_prompt", "prompt", "candidate_prompt") or "").strip()
    if not prompt:
        raise ValueError(f"case {case_id}: user_prompt is required")

    responses = value.get("responses")
    if responses is not None and not isinstance(responses, dict):
        raise ValueError(f"case {case_id}: responses must be an object")
    responses = responses or {}
    on = _pick(
        value,
        "tool_response_on",
        "injection_on_response",
        "tool_response",
    )
    if on is None:
        on = responses.get("on")
    off = _pick(value, "tool_response_off", "injection_off_response", "control_response")
    if off is None:
        off = responses.get("off")
    if not isinstance(on, str) or not on.strip():
        raise ValueError(f"case {case_id}: tool_response_on is required")
    if off is not None and not isinstance(off, str):
        raise ValueError(f"case {case_id}: tool_response_off must be text")

    reserved = {
        "case_id",
        "id",
        "chain_id",
        "chain",
        "user_prompt",
        "prompt",
        "candidate_prompt",
        "tool_response_on",
        "injection_on_response",
        "tool_response",
        "tool_response_off",
        "injection_off_response",
        "control_response",
        "responses",
        "prompt_condition",
        "condition",
        "target_template",
        "injection_target",
        "metadata",
    }
    metadata = dict(value.get("metadata") or {})
    metadata.update({key: item for key, item in value.items() if key not in reserved})
    authorization_cue = str(metadata.get("authorization_cue") or "none")
    if authorization_cue not in {"none", "implicit", "explicit"}:
        raise ValueError(f"case {case_id}: invalid authorization_cue")
    metadata["authorization_cue"] = authorization_cue
    return ExperimentCase(
        case_id=case_id,
        chain_id=chain_id,
        user_prompt=prompt,
        tool_response_on=on,
        tool_response_off=off,
        prompt_condition=str(
            _pick(value, "prompt_condition", "condition") or "corpus"
        ),
        target_template=(
            str(_pick(value, "target_template", "injection_target"))
            if _pick(value, "target_template", "injection_target") is not None
            else None
        ),
        metadata=metadata,
    )


def _read_payload(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path.suffix.lower() == ".jsonl":
        values: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{line_no}: record must be an object")
                values.append(value)
        return {}, values

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return {}, payload
    if not isinstance(payload, dict):
        raise ValueError("corpus root must be an object or array")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("JSON corpus object must contain a cases array")
    metadata = {key: value for key, value in payload.items() if key != "cases"}
    return metadata, cases


def load_corpus(path: Path) -> tuple[dict[str, Any], list[ExperimentCase]]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"corpus does not exist: {resolved}")
    if resolved.suffix.lower() == ".xlsx":
        from .sensitive_corpus import load_sensitive_corpus

        return load_sensitive_corpus(resolved)
    if resolved.name == "organized_chain_cases.json":
        from .organized_corpus import load_organized_corpus

        return load_organized_corpus(resolved)
    metadata, raw_cases = _read_payload(resolved)
    cases: list[ExperimentCase] = []
    seen: set[str] = set()
    for index, value in enumerate(raw_cases, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"case {index}: record must be an object")
        case = _case_from_dict(value, index=index)
        if case.case_id in seen:
            raise ValueError(f"duplicate case_id: {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise ValueError("corpus contains no cases")
    return metadata, cases


def corpus_index(path: Path) -> dict[str, ExperimentCase]:
    return {case.case_id: case for case in load_corpus(path)[1]}
