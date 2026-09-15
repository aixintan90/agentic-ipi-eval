from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import CHAINS_CONFIG, EVALUATOR_CONFIG


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ChainSpec:
    chain_id: str
    name_zh: str
    sink: str
    sink_kind: str
    oracle: str
    f_event_kind: str
    injection_target: str
    injection_on: str
    injection_off: str
    safe_scope: str
    prerequisites: tuple[str, ...]
    process_patterns: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    source_to_sink_call_chain: tuple[str, ...]
    source_tool: str = "begin"

    @classmethod
    def from_dict(cls, value: dict[str, Any], source_tool: str) -> ChainSpec:
        required = {
            "chain_id",
            "name_zh",
            "sink",
            "sink_kind",
            "oracle",
            "f_event_kind",
            "injection_target",
            "injection_on",
            "injection_off",
            "safe_scope",
            "source_to_sink_call_chain",
        }
        missing = sorted(required - value.keys())
        if missing:
            raise ConfigError(f"{value.get('chain_id', '<unknown>')}: missing {missing}")
        if value["sink_kind"] not in {"builtin", "mcp"}:
            raise ConfigError(f"{value['chain_id']}: invalid sink_kind")
        call_chain = value["source_to_sink_call_chain"]
        if not isinstance(call_chain, list) or len(call_chain) < 2:
            raise ConfigError(
                f"{value['chain_id']}: source_to_sink_call_chain must contain at least two nodes"
            )
        return cls(
            chain_id=str(value["chain_id"]),
            name_zh=str(value["name_zh"]),
            sink=str(value["sink"]),
            sink_kind=str(value["sink_kind"]),
            oracle=str(value["oracle"]),
            f_event_kind=str(value["f_event_kind"]),
            injection_target=str(value["injection_target"]),
            injection_on=str(value["injection_on"]),
            injection_off=str(value["injection_off"]),
            safe_scope=str(value["safe_scope"]),
            prerequisites=tuple(map(str, value.get("prerequisites", []))),
            process_patterns=tuple(map(str, value.get("process_patterns", []))),
            evidence_ids=tuple(map(str, value.get("evidence_ids", []))),
            source_to_sink_call_chain=tuple(map(str, call_chain)),
            source_tool=source_tool,
        )


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: root must be an object")
    return value


def load_chain_specs(path: Path = CHAINS_CONFIG) -> dict[str, ChainSpec]:
    root = load_json(path)
    if root.get("schema_version") != "1.0":
        raise ConfigError(f"{path}: unsupported schema_version")
    source_tool = str(root.get("source_tool", "begin"))
    raw_chains = root.get("chains")
    if not isinstance(raw_chains, list):
        raise ConfigError(f"{path}: chains must be a list")
    specs: dict[str, ChainSpec] = {}
    for raw in raw_chains:
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: each chain must be an object")
        spec = ChainSpec.from_dict(raw, source_tool=source_tool)
        if spec.chain_id in specs:
            raise ConfigError(f"{path}: duplicate {spec.chain_id}")
        specs[spec.chain_id] = spec
    expected = {f"CHAIN-{index:02d}" for index in range(1, 10)}
    if set(specs) != expected:
        raise ConfigError(f"{path}: expected CHAIN-01..09, got {sorted(specs)}")
    return specs


def load_evaluator_config(path: Path = EVALUATOR_CONFIG) -> dict[str, Any]:
    value = load_json(path)
    if value.get("schema_version") != "1.0":
        raise ConfigError(f"{path}: unsupported schema_version")
    return value


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def render_template(template: str, variables: dict[str, object]) -> str:
    rendered = template
    for name, value in variables.items():
        rendered = rendered.replace("{{" + name + "}}", str(value))
    unresolved = [part.split("}}", 1)[0] for part in rendered.split("{{")[1:] if "}}" in part]
    if unresolved:
        raise ConfigError(f"unresolved template variables: {unresolved}")
    return rendered


def validate_all_configs() -> dict[str, object]:
    specs = load_chain_specs()
    evaluator = load_evaluator_config()
    return {
        "chains": len(specs),
        "chain_ids": sorted(specs),
        "settle": evaluator.get("settle", {}),
        "aggregation": evaluator.get("aggregation", {}),
    }
