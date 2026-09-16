"""Local import and normalization for optional direct-replay prompt files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..automation.corpus import CASE_ID_PATTERN

MAX_BASELINE_PROMPTS = 20_000
MAX_PROMPT_LENGTH = 20_000


def _records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        values = []
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"第 {line_no} 行不是 JSON 对象")
            values.append(value)
        return values
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("Prompt 文件必须是 JSON 数组、带列表的 JSON 对象或 JSONL")
    for key in ("successful_prompts", "prompts", "items", "cases"):
        if isinstance(payload.get(key), list):
            return payload[key]
    if payload.get("case_id"):
        return [payload]
    raise ValueError("JSON 中没有找到 successful_prompts、prompts、items 或 cases 列表")


def load_baseline_prompts(path: Path) -> list[dict[str, Any]]:
    """Accept existing successful-prompt exports and return a minimal stable form."""

    resolved = path.resolve()
    if not resolved.is_file() or resolved.suffix.lower() not in {".json", ".jsonl"}:
        raise ValueError("请选择存在的 JSON 或 JSONL 成功 Prompt 文件")
    raw = _records(resolved)
    if not raw:
        raise ValueError("成功 Prompt 文件为空")
    if len(raw) > MAX_BASELINE_PROMPTS:
        raise ValueError(f"成功 Prompt 文件最多支持 {MAX_BASELINE_PROMPTS} 条")
    prompts: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条不是 JSON 对象")
        case_id = str(row.get("case_id") or row.get("id") or "").strip()
        prompt = str(
            row.get("prompt")
            or row.get("successful_prompt")
            or row.get("user_prompt")
            or ""
        ).strip()
        if not CASE_ID_PATTERN.fullmatch(case_id):
            raise ValueError(f"第 {index} 条缺少有效 case_id")
        if not prompt:
            raise ValueError(f"{case_id} 缺少 prompt 或 successful_prompt")
        if len(prompt) > MAX_PROMPT_LENGTH:
            raise ValueError(f"{case_id} 的 Prompt 超过 {MAX_PROMPT_LENGTH} 个字符")
        if case_id in seen:
            if seen[case_id] != prompt:
                raise ValueError(f"{case_id} 存在多个不同的成功 Prompt")
            continue
        seen[case_id] = prompt
        prompts.append(
            {
                "case_id": case_id,
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "source_model": str(
                    row.get("base_model") or row.get("model_id") or row.get("model") or ""
                ).strip(),
                "source_p_type": str(row.get("p_type") or "").strip(),
                "source_round": row.get("round"),
            }
        )
    return prompts


def prompt_index(path: Path) -> dict[str, dict[str, Any]]:
    return {row["case_id"]: row for row in load_baseline_prompts(path)}

