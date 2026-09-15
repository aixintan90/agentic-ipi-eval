"""Portable, strict experiment configuration. Secrets are environment references only."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..automation.cli_runner import PathBridge
from ..automation.corpus import load_corpus
from .models import DEFAULT_CURSOR_MODEL


def defaults(project: Path) -> dict:
    from .egress import settings

    bridge = "wsl" if os.name == "nt" else "native"
    python_path = project / (".venv-wsl/bin/python" if bridge == "wsl" else ".venv/bin/python")
    internal_corpus = project / "config/corpora/teacher_new_windows_full.json"
    bundled_corpus = (
        "config/corpora/teacher_new_windows_full.json"
        if internal_corpus.is_file()
        else "config/corpora/workbench_example.json"
    )
    return {
        "schema_version": "experiment/1.0",
        "name": "Windows 完全授权实验",
        "corpus": bundled_corpus,
        "platform": "windows",
        "authorization": "full",
        "injection": "on",
        "target": {
            "adapter": "cursor_cli",
            "model": DEFAULT_CURSOR_MODEL,
            "bridge": bridge,
            "wsl_distro": "Ubuntu",
            "mcp_python": PathBridge(mode=bridge).to_cli_path(python_path),
        },
        "generation": {
            "strategy": "thought_tree",
            "base_url": "https://xiaoai.plus/v1",
            "model": "deepseek-v4-flash",
            "api_key_env": "SP27_MUTATION_API_KEY",
            "budgets": [8, 4, 2],
            "early_stop": True,
            "concurrency": 4,
            "timeout_seconds": 60,
        },
        "execution": {
            "workers": 8,
            "timeout_seconds": 300,
            "infrastructure_retries": 4,
            "export_every": 10,
            "runtime_root": "",
        },
        "evaluation": {"metric": "actual_effect_verified"},
        "egress": settings(),
    }


def normalize(payload: dict, project: Path) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("配置必须是 JSON 对象")
    result = defaults(project)
    legacy_egress = payload.get("egress") if isinstance(payload, dict) else None
    if isinstance(legacy_egress, dict) and "email_transport" not in legacy_egress:
        # Pre-0.1.3 configs used authenticated SMTP exclusively.
        result["egress"]["email_transport"] = "authenticated_smtp"
    for key, value in payload.items():
        if key not in result:
            raise ValueError(f"未知配置字段：{key}")
        if isinstance(result[key], dict):
            if not isinstance(value, dict) or set(value) - set(result[key]):
                raise ValueError(f"配置分组 {key} 含有未知字段或类型错误")
            result[key].update(copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)
    if result["schema_version"] != "experiment/1.0":
        raise ValueError("不支持的配置版本")
    for key in ("name", "corpus"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f"{key} 不能为空")
    if len(result["name"]) > 160:
        raise ValueError("实验名称最多 160 个字符")
    if result["platform"] not in {"windows", "linux", "macos"}:
        raise ValueError("不支持的平台")
    if result["authorization"] != "full" or result["injection"] not in {"on", "off"}:
        raise ValueError("当前执行协议支持完全授权和 injection on/off")
    target, generation, execution = (result[k] for k in ("target", "generation", "execution"))
    for key in ("adapter", "model", "mcp_python", "wsl_distro"):
        if not isinstance(target[key], str) or not target[key].strip():
            raise ValueError(f"target.{key} 不能为空")
    if target["bridge"] not in {"native", "wsl"}:
        raise ValueError("bridge 必须是 native 或 wsl")
    if generation["strategy"] != "thought_tree":
        raise ValueError("当前生成策略为 thought_tree")
    if not isinstance(generation["model"], str) or not generation["model"].strip():
        raise ValueError("变异模型不能为空")
    url = urlsplit(str(generation["base_url"]))
    if (
        url.scheme not in {"https", "http"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("API 地址应是无凭据、无查询参数的 HTTP(S) 基址")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(generation["api_key_env"])):
        raise ValueError("api_key_env 必须是环境变量名，不能填写密钥")
    budgets = generation["budgets"]
    if (
        not isinstance(budgets, list)
        or not 1 <= len(budgets) <= 10
        or any(type(n) is not int or not 1 <= n <= 64 for n in budgets)
    ):
        raise ValueError("每轮预算应为 1–64 的整数，最多 10 轮")
    if type(generation["early_stop"]) is not bool:
        raise ValueError("early_stop 必须是布尔值")
    for group, key, low, high in (
        (execution, "workers", 1, 48),
        (execution, "infrastructure_retries", 0, 10),
        (execution, "export_every", 1, 100),
        (generation, "concurrency", 1, 16),
    ):
        if type(group[key]) is not int or not low <= group[key] <= high:
            raise ValueError(f"{key} 必须在 {low}–{high} 之间")
    for group in (generation, execution):
        if (
            type(group["timeout_seconds"]) not in (int, float)
            or not 5 <= group["timeout_seconds"] <= 3600
        ):
            raise ValueError("timeout_seconds 必须在 5–3600 之间")
    if not isinstance(execution["runtime_root"], str):
        raise ValueError("runtime_root 必须是路径字符串")
    if execution["runtime_root"] and not Path(execution["runtime_root"]).is_absolute():
        raise ValueError("runtime_root 请使用本机绝对路径或留空")
    if result["evaluation"]["metric"] not in {
        "actual_effect_verified",
        "original_sink_intent",
        "proxy_effect_verified",
    }:
        raise ValueError("不支持的成功指标")
    from .egress import secret_names, validate

    validate(result["egress"])
    if generation["api_key_env"] in secret_names(result["egress"]):
        raise ValueError("变异 API 与传输服务不能共用密钥变量")
    return result


def corpus_path(config: dict, project: Path) -> Path:
    path = Path(config["corpus"])
    return (path if path.is_absolute() else project / path).resolve()


def inspect_corpus(config: dict, project: Path) -> tuple[Path, list, dict]:
    from collections import Counter

    from ..automation.semantic_corpus import assert_semantic_case_runnable

    path = corpus_path(config, project)
    _, cases = load_corpus(path)
    if not cases:
        raise ValueError("语料不能为空")
    for case in cases:
        assert_semantic_case_runnable(case.metadata, case.case_id)
        if config["injection"] not in case.available_injections():
            raise ValueError(f"{case.case_id} 缺少 {config['injection']} 内容")
        platform = case.metadata.get("platform")
        if platform and platform != config["platform"]:
            raise ValueError(f"{case.case_id} 平台与实验配置不一致")
    return (
        path,
        cases,
        {
            "case_count": len(cases),
            "corpus_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "categories": dict(
                Counter(c.metadata.get("attack_category", "unspecified") for c in cases)
            ),
        },
    )


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def engine_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    # Frozen builds carry this value from the source tree; bytecode paths vary per install.
    packaged = Path(__file__).parent / "engine-fingerprint.txt"
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8").strip()
    files = sorted(root.rglob("*.py"))
    return digest(
        {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    )
