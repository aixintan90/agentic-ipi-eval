from __future__ import annotations

import hashlib
import json
import re
import secrets
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import ChainSpec, render_template

RUN_ID_PATTERN = re.compile(r"^r\d{8}T\d{6}Z-[0-9a-f]{8}$")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def new_run_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"r{stamp}-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class RunContext:
    run_id: str
    chain_id: str
    prompt_id: str
    prompt_condition: str
    injection: str
    round: int
    retained_from: str | None
    workspace: Path
    http_port: int
    started_at: float
    injection_target: str
    tool_response: str
    source_tool: str
    safe_scope: str
    case_id: str | None = None
    remote_case_id: str | None = None
    user_prompt: str = ""
    authorization_cue: str = "none"
    tool_description_condition: str = "specific"
    read_canary_token: str = ""
    read_canary_protocol: str = "random-v1"
    sensitive_egress_expected: bool = False
    controlled_egress_kind: str = ""
    controlled_egress_recipient: str = ""

    @classmethod
    def create(
        cls,
        spec: ChainSpec,
        *,
        prompt_id: str,
        prompt_condition: str,
        injection: str,
        workspace: Path,
        round_no: int = 1,
        retained_from: str | None = None,
        run_id: str | None = None,
        http_port: int | None = None,
        started_at: float | None = None,
        case_id: str | None = None,
        remote_case_id: str | None = None,
        user_prompt: str = "",
        tool_response_template: str | None = None,
        target_template: str | None = None,
        authorization_cue: str = "none",
        tool_description_condition: str = "specific",
        sensitive_egress_expected: bool = False,
        controlled_egress_kind: str = "",
        controlled_egress_recipient: str = "",
    ) -> RunContext:
        resolved_workspace = workspace.resolve()
        if not resolved_workspace.is_dir():
            raise ValueError(f"workspace does not exist: {resolved_workspace}")
        actual_run_id = run_id or new_run_id()
        if not RUN_ID_PATTERN.match(actual_run_id):
            raise ValueError(f"invalid run_id: {actual_run_id}")
        if injection not in {"on", "off"}:
            raise ValueError("injection must be on or off")
        if authorization_cue not in {"none", "implicit", "explicit"}:
            raise ValueError("invalid authorization_cue")
        if tool_description_condition not in {"specific", "generalized"}:
            raise ValueError("invalid tool_description_condition")
        if controlled_egress_kind not in {"", "marker", "email", "scp"}:
            raise ValueError("invalid controlled_egress_kind")
        if controlled_egress_kind == "email" and not controlled_egress_recipient:
            raise ValueError("controlled email requires a recipient")
        read_canary_token = f"READ_CANARY_{secrets.token_hex(16)}"
        port = int(http_port or _free_local_port())
        variables: dict[str, object] = {
            "run_id": actual_run_id,
            "workspace": resolved_workspace.as_posix(),
            "http_port": port,
        }
        target = render_template(target_template or spec.injection_target, variables)
        variables["target"] = target
        template = tool_response_template
        if template is None:
            template = spec.injection_on if injection == "on" else spec.injection_off
        response = render_template(template, variables)
        if spec.chain_id == "CHAIN-02" and "READ_CANARY_" in response:
            raise ValueError("CHAIN-02 Tool Response must not disclose the read canary")
        if injection == "off" and tool_response_template is None:
            on_length = len(render_template(spec.injection_on, variables))
            response = response.ljust(on_length)
        return cls(
            run_id=actual_run_id,
            chain_id=spec.chain_id,
            prompt_id=prompt_id,
            prompt_condition=prompt_condition,
            injection=injection,
            round=int(round_no),
            retained_from=retained_from,
            workspace=resolved_workspace,
            http_port=port,
            started_at=float(started_at or time.time()),
            injection_target=target,
            tool_response=response,
            source_tool=spec.source_tool,
            safe_scope=spec.safe_scope,
            case_id=case_id,
            remote_case_id=remote_case_id,
            user_prompt=user_prompt,
            authorization_cue=authorization_cue,
            tool_description_condition=tool_description_condition,
            read_canary_token=read_canary_token,
            read_canary_protocol="random-v1",
            sensitive_egress_expected=bool(sensitive_egress_expected),
            controlled_egress_kind=controlled_egress_kind,
            controlled_egress_recipient=controlled_egress_recipient,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "chain_id": self.chain_id,
            "prompt_id": self.prompt_id,
            "prompt_condition": self.prompt_condition,
            "injection": self.injection,
            "round": self.round,
            "retained_from": self.retained_from,
            "workspace": str(self.workspace),
            "http_port": self.http_port,
            "started_at": self.started_at,
            "injection_target": self.injection_target,
            "tool_response": self.tool_response,
            "source_tool": self.source_tool,
            "safe_scope": self.safe_scope,
            "case_id": self.case_id,
            "remote_case_id": self.remote_case_id,
            "user_prompt": self.user_prompt,
            "user_prompt_sha256": hashlib.sha256(
                self.user_prompt.encode("utf-8")
            ).hexdigest(),
            "authorization_cue": self.authorization_cue,
            "tool_description_condition": self.tool_description_condition,
            "read_canary_token": self.read_canary_token,
            "read_canary_protocol": self.read_canary_protocol,
            "read_canary_token_sha256": hashlib.sha256(
                self.read_canary_token.encode()
            ).hexdigest(),
            "sensitive_egress_expected": self.sensitive_egress_expected,
            "controlled_egress_kind": self.controlled_egress_kind,
            "controlled_egress_recipient": self.controlled_egress_recipient,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunContext:
        return cls(
            run_id=str(value["run_id"]),
            chain_id=str(value["chain_id"]),
            prompt_id=str(value["prompt_id"]),
            prompt_condition=str(value["prompt_condition"]),
            injection=str(value["injection"]),
            round=int(value["round"]),
            retained_from=value.get("retained_from"),
            workspace=Path(value["workspace"]).resolve(),
            http_port=int(value["http_port"]),
            started_at=float(value["started_at"]),
            injection_target=str(value["injection_target"]),
            tool_response=str(value["tool_response"]),
            source_tool=str(value["source_tool"]),
            safe_scope=str(value["safe_scope"]),
            case_id=(str(value["case_id"]) if value.get("case_id") else None),
            remote_case_id=(
                str(value["remote_case_id"])
                if value.get("remote_case_id")
                else None
            ),
            user_prompt=str(value.get("user_prompt") or ""),
            authorization_cue=str(value.get("authorization_cue") or "none"),
            tool_description_condition=str(
                value.get("tool_description_condition") or "specific"
            ),
            read_canary_token=str(
                value.get("read_canary_token")
                or f"READ_CANARY_{value['run_id']}"
            ),
            read_canary_protocol=str(
                value.get("read_canary_protocol") or "legacy-deterministic-v0"
            ),
            sensitive_egress_expected=bool(value.get("sensitive_egress_expected")),
            controlled_egress_kind=str(value.get("controlled_egress_kind") or ""),
            controlled_egress_recipient=str(
                value.get("controlled_egress_recipient") or ""
            ),
        )

    def write_manifest(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    @classmethod
    def read_manifest(cls, path: Path) -> RunContext:
        with path.open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))
