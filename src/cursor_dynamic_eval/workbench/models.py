"""Read-only CLI model discovery; never infer, substitute or call a model."""

from __future__ import annotations

import os
import re
import shlex
import subprocess

from ..automation.cli_runner import PathBridge
from .storage import now

DEFAULT_CURSOR_MODEL = "cursor-grok-4.6-high"


def parse_cursor_models(output: str) -> list[dict]:
    # The installed CLI prints `Available models`, then `id - display name`.
    # Fail closed if that protocol changes, instead of guessing from diagnostics.
    clean = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", output)
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", clean).replace("\x00", "")
    rows, seen, in_list = [], set(), False
    for line in clean.splitlines():
        line = line.strip()
        if line == "Available models":
            in_list = True
            continue
        if not in_list:
            continue
        if line.startswith("Tip:"):
            break
        match = re.fullmatch(r"([a-zA-Z0-9][a-zA-Z0-9._-]*)\s+-\s+(.+)", line)
        if not match or match[1] in seen:
            continue
        identifier, label = match.groups()
        label = re.sub(r"\s+\((?:current|default)(?:, (?:current|default))*\)$", "", label)
        seen.add(identifier)
        rows.append({"id": identifier, "label": label})
    if not rows:
        raise ValueError("未读到有效模型列表。请检查 Cursor 登录、CLI 版本与网络，再刷新。")
    return rows


def cursor_models(config: dict) -> dict:
    from .egress import secret_names, settings

    target = config["target"]
    excluded = {
        *secret_names(config.get("egress", settings())),
        config["generation"]["api_key_env"],
        "SP27_MUTATION_API_KEY",
        "XIAOAI_API_KEY",
    }
    bridge = PathBridge(mode=target["bridge"], distro=target["wsl_distro"])
    argv = ["cursor-agent", "--list-models"]
    if bridge.mode == "wsl":
        # WSLENV can explicitly forward host secrets, so remove their forwarding too.
        command = 'export PATH="$HOME/.local/bin:$PATH"; ' + shlex.join(argv)
        argv = [*bridge.command_prefix(), "bash", "-lc", command]
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    if "WSLENV" in env:
        env["WSLENV"] = ":".join(
            entry for entry in env["WSLENV"].split(":") if entry.split("/")[0] not in excluded
        )
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("读取模型列表超时（30 秒）。请检查网络后刷新。") from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError("无法启动 Cursor CLI。请核对运行环境和 WSL 名称。") from None
    if result.returncode != 0:
        # Do not return raw stderr: CLI diagnostics can include account credentials.
        raise ValueError("Cursor 未返回模型列表。请检查对应环境的登录状态与网络后刷新。")
    return {
        "models": parse_cursor_models(result.stdout),
        "checked_at": now(),
        "source": "cursor-agent --list-models",
    }
