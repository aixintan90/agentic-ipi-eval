"""Protocol adapters for controlled sensitive-disclosure experiments.

The adapters deliberately accept only a run-scoped synthetic canary.  SMTP is
sent through an SSH tunnel to a loopback-only capture service; SCP uses a
dedicated non-root account and an allowlisted remote directory.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import smtplib
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class ControlledEgressConfig:
    smtp_ssh_host: str
    smtp_ssh_port: int
    smtp_ssh_user: str
    smtp_ssh_key: Path
    smtp_known_hosts: Path
    smtp_remote_host: str
    smtp_remote_port: int
    scp_host: str
    scp_port: int
    scp_user: str
    scp_key: Path
    scp_known_hosts: Path
    scp_remote_directory: str
    ssh_executable: str = "ssh"
    scp_executable: str = "scp"
    mode: str = "ssh_capture"
    local_capture_root: Path | None = None
    email_mx_hosts: tuple[str, ...] = ()
    email_helo_name: str = "sp27-controlled.local"
    email_sender_header: str = "SP27 controlled experiment <no-reply@example.invalid>"

    @classmethod
    def from_path(cls, path: Path) -> ControlledEgressConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid controlled egress configuration: {path}") from exc
        mode = str(raw.get("mode") or "ssh_capture")
        if mode == "local_capture":
            configured = Path(str(raw.get("capture_root") or ""))
            if not str(configured):
                raise ValueError("local controlled egress capture_root is missing")
            capture_root = configured if configured.is_absolute() else (path.parent / configured)
            return cls(
                smtp_ssh_host="",
                smtp_ssh_port=0,
                smtp_ssh_user="",
                smtp_ssh_key=Path(),
                smtp_known_hosts=Path(),
                smtp_remote_host="127.0.0.1",
                smtp_remote_port=0,
                scp_host="",
                scp_port=0,
                scp_user="local-controlled-capture",
                scp_key=Path(),
                scp_known_hosts=Path(),
                scp_remote_directory="local-controlled-capture",
                ssh_executable=str(raw.get("ssh_executable") or "ssh"),
                scp_executable=str(raw.get("scp_executable") or "scp"),
                mode=mode,
                local_capture_root=capture_root.resolve(),
            )
        if mode == "direct_mx_scp":
            try:
                email = dict(raw["email"])
                scp = dict(raw["scp"])
                mx_hosts = tuple(str(item) for item in email["mx_hosts"] if str(item))
            except (KeyError, TypeError) as exc:
                raise ValueError(f"invalid controlled egress configuration: {path}") from exc
            if not mx_hosts:
                raise ValueError("direct MX controlled egress requires at least one MX host")
            result = cls(
                smtp_ssh_host="",
                smtp_ssh_port=0,
                smtp_ssh_user="",
                smtp_ssh_key=Path(),
                smtp_known_hosts=Path(),
                smtp_remote_host="",
                smtp_remote_port=int(email.get("port") or 25),
                scp_host=str(scp["host"]),
                scp_port=int(scp["port"]),
                scp_user=str(scp["user"]),
                scp_key=Path(str(scp["ssh_key"])).expanduser(),
                scp_known_hosts=Path(str(scp["known_hosts"])).expanduser(),
                scp_remote_directory=str(scp["remote_directory"]),
                ssh_executable=str(raw.get("ssh_executable") or "ssh"),
                scp_executable=str(raw.get("scp_executable") or "scp"),
                mode=mode,
                email_mx_hosts=mx_hosts,
                email_helo_name=str(email.get("helo_name") or "sp27-controlled.local"),
                email_sender_header=str(
                    email.get("sender_header")
                    or "SP27 controlled experiment <no-reply@example.invalid>"
                ),
            )
            if not result.scp_key.is_file() or not result.scp_known_hosts.is_file():
                raise ValueError("real SCP SSH key or known_hosts is unavailable")
            if result.scp_remote_directory != "/incoming":
                raise ValueError("real SCP destination must be the dedicated /incoming directory")
            return result
        if mode != "ssh_capture":
            raise ValueError(f"unsupported controlled egress mode: {mode}")
        try:
            smtp = dict(raw["smtp"])
            scp = dict(raw["scp"])
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid controlled egress configuration: {path}") from exc
        result = cls(
            smtp_ssh_host=str(smtp["ssh_host"]),
            smtp_ssh_port=int(smtp["ssh_port"]),
            smtp_ssh_user=str(smtp["ssh_user"]),
            smtp_ssh_key=Path(str(smtp["ssh_key"])).expanduser(),
            smtp_known_hosts=Path(
                str(smtp.get("known_hosts") or raw.get("known_hosts") or "")
            ).expanduser(),
            smtp_remote_host=str(smtp.get("remote_host") or "127.0.0.1"),
            smtp_remote_port=int(smtp.get("remote_port") or 2525),
            scp_host=str(scp["host"]),
            scp_port=int(scp["port"]),
            scp_user=str(scp["user"]),
            scp_key=Path(str(scp["ssh_key"])).expanduser(),
            scp_known_hosts=Path(
                str(scp.get("known_hosts") or raw.get("known_hosts") or "")
            ).expanduser(),
            scp_remote_directory=str(scp["remote_directory"]),
            ssh_executable=str(raw.get("ssh_executable") or "ssh"),
            scp_executable=str(raw.get("scp_executable") or "scp"),
            mode=mode,
        )
        if not result.smtp_ssh_key.is_file() or not result.scp_key.is_file():
            raise ValueError("controlled egress SSH key is unavailable")
        if not result.smtp_known_hosts.is_file() or not result.scp_known_hosts.is_file():
            raise ValueError("controlled egress known_hosts file is unavailable")
        if not result.scp_remote_directory.startswith("/hy-tmp/ipi-controlled-egress/"):
            raise ValueError("SCP destination is outside the controlled egress directory")
        return result


def _capture_local_metadata(
    config: ControlledEgressConfig,
    *,
    kind: str,
    run_id: str,
    value: str,
    recipient_hash: str = "",
) -> dict[str, Any]:
    root = config.local_capture_root
    if config.mode != "local_capture" or root is None:
        raise ValueError("local controlled egress is not configured")
    root.mkdir(parents=True, exist_ok=True)
    record = {
        "kind": kind,
        "run_id": run_id,
        "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
        "value_size": len(value.encode()),
        "recipient_sha256": recipient_hash,
        "payload_stored": False,
        "external_contact": False,
    }
    with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _wait_for_tunnel(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("controlled SMTP SSH tunnel exited before becoming ready")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError("controlled SMTP SSH tunnel did not become ready")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def capture_email(
    config: ControlledEgressConfig,
    *,
    run_id: str,
    value: str,
    recipient: str,
) -> dict[str, Any]:
    if config.mode == "local_capture":
        captured = _capture_local_metadata(
            config,
            kind="email",
            run_id=run_id,
            value=value,
            recipient_hash=hashlib.sha256(recipient.casefold().encode()).hexdigest(),
        )
        return {
            "recipient": "controlled-recipient",
            "message_sha256": captured["value_sha256"],
            "value_sha256": captured["value_sha256"],
            "transport": "local_hash_only_email_capture",
            "relayed": False,
        }
    if config.mode == "direct_mx_scp":
        message = EmailMessage()
        message["From"] = config.email_sender_header
        message["To"] = recipient
        message["Subject"] = "SP27 controlled experiment synthetic canary"
        message["X-Cursor-IPI-Run"] = run_id
        message.set_content(value)
        payload = message.as_bytes()
        last_error: Exception | None = None
        for host in config.email_mx_hosts:
            try:
                with smtplib.SMTP(host, config.smtp_remote_port, timeout=15.0) as client:
                    client.ehlo(config.email_helo_name)
                    refused = client.sendmail("", [recipient], payload)
                if not refused:
                    return {
                        "recipient": "teacher-approved-recipient",
                        "message_sha256": hashlib.sha256(payload).hexdigest(),
                        "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
                        "transport": "direct_mx_smtp_delivery",
                        "relayed": True,
                        "external_contact": True,
                    }
            except (OSError, smtplib.SMTPException) as exc:
                last_error = exc
        raise RuntimeError(
            "real SMTP delivery was not accepted by any configured MX"
        ) from last_error
    local_port = _free_port()
    command = [
        config.ssh_executable,
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={config.smtp_known_hosts}",
        "-i",
        str(config.smtp_ssh_key),
        "-p",
        str(config.smtp_ssh_port),
        "-L",
        f"127.0.0.1:{local_port}:{config.smtp_remote_host}:{config.smtp_remote_port}",
        f"{config.smtp_ssh_user}@{config.smtp_ssh_host}",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_for_tunnel(process, local_port)
        message = EmailMessage()
        message["From"] = "cursor-ipi-controlled@invalid"
        message["To"] = recipient
        message["Subject"] = "Controlled IPI synthetic canary"
        message["X-Cursor-IPI-Run"] = run_id
        message.set_content(value)
        payload = message.as_bytes()
        with smtplib.SMTP("127.0.0.1", local_port, timeout=12.0) as client:
            client.sendmail(str(message["From"]), [recipient], payload)
    finally:
        _stop(process)
    return {
        "recipient": recipient,
        "message_sha256": hashlib.sha256(payload).hexdigest(),
        "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
        "transport": "smtp_capture_over_ssh_tunnel",
        "relayed": False,
    }


def capture_scp_upload(
    config: ControlledEgressConfig,
    *,
    run_id: str,
    value: str,
) -> dict[str, Any]:
    if config.mode == "local_capture":
        captured = _capture_local_metadata(
            config,
            kind="scp",
            run_id=run_id,
            value=value,
        )
        return {
            "destination": f"controlled-local-scp:{run_id}.txt",
            "value_sha256": captured["value_sha256"],
            "transport": "local_hash_only_scp_capture",
            "remote_user": "local-controlled-capture",
            "external_contact": False,
        }
    filename = f"{run_id}.txt"
    remote_path = f"{config.scp_remote_directory.rstrip('/')}/{filename}"
    destination = f"{config.scp_user}@{config.scp_host}:{remote_path}"
    expected_file_sha256 = ""
    with tempfile.TemporaryDirectory(prefix="cursor-ipi-scp-") as temporary:
        source = Path(temporary) / filename
        # Use bytes so the remote digest is platform-independent (Windows
        # text-mode newline translation would otherwise change the evidence).
        source.write_bytes((value + "\n").encode("utf-8"))
        expected_file_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        completed = subprocess.run(
            [
                config.scp_executable,
                "-P",
                str(config.scp_port),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={config.scp_known_hosts}",
                "-i",
                str(config.scp_key),
                str(source),
                destination,
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    if completed.returncode != 0:
        raise RuntimeError("controlled SCP upload failed")
    verified = subprocess.run(
        [
            config.ssh_executable,
            "-p",
            str(config.scp_port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={config.scp_known_hosts}",
            "-i",
            str(config.scp_key),
            f"{config.scp_user}@{config.scp_host}",
            f"sha256sum -- {shlex.quote(remote_path)}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    remote_digest = verified.stdout.decode("ascii", errors="ignore").strip().split()
    if (
        verified.returncode != 0
        or not remote_digest
        or remote_digest[0].casefold() != expected_file_sha256.casefold()
    ):
        raise RuntimeError("controlled SCP remote hash verification failed")
    return {
        "destination": f"controlled-scp:{filename}",
        "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
        "transport": "scp_to_dedicated_real_sink",
        "remote_user": config.scp_user,
        "external_contact": config.mode == "direct_mx_scp",
        "remote_verified": True,
        "remote_file_sha256": expected_file_sha256,
    }
