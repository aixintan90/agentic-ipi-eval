"""Real transports for a fixed, run-scoped synthetic marker, never arbitrary files.

The model's process cannot access credentials or choose the endpoint/payload. A
parent-side broker requires the recorded MCP request and source-read evidence.
SMTP acceptance is deliberately not represented as inbox delivery confirmation.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import smtplib
import socket
import ssl
import subprocess
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path, PurePosixPath

from ..automation.formal_checkpoint import exclusive_batch
from .storage import now, read_json, write_json


def settings() -> dict:
    return {
        "mode": "local_capture",
        "email_enabled": False,
        "email_transport": "direct_mx",
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_security": "tls",
        "smtp_user": "",
        "smtp_password_env": "SP27_SMTP_PASSWORD",
        "sender": "",
        "recipient": "",
        "upload_enabled": False,
        "ssh_host": "",
        "ssh_port": 22,
        "ssh_user": "",
        "ssh_password_env": "SP27_SFTP_PASSWORD",
        "ssh_host_key_sha256": "",
        "remote_directory": "",
    }


def validate(profile: dict, *, complete=False) -> None:
    expected = settings()
    if set(profile) != set(expected):
        raise ValueError("邮件/上传配置字段不完整或含有未知字段")
    for key, default in expected.items():
        if type(profile[key]) is not type(default):
            raise ValueError(f"邮件/上传字段类型错误：{key}")
    if profile["mode"] not in {"local_capture", "controlled_remote"}:
        raise ValueError("不支持的邮件/上传模式")
    if profile["email_transport"] not in {"direct_mx", "authenticated_smtp"}:
        raise ValueError("不支持的邮件发送方式")
    if profile["smtp_security"] not in {"tls", "starttls"}:
        raise ValueError("SMTP 必须使用 TLS 或 STARTTLS")
    for key in ("smtp_port", "ssh_port"):
        if not 1 <= profile[key] <= 65535:
            raise ValueError(f"{key} 必须在 1–65535 之间")
    for key in ("smtp_password_env", "ssh_password_env"):
        if not re.fullmatch(r"SP27_[A-Z0-9_]+", profile[key]):
            raise ValueError("传输密钥变量必须使用 SP27_ 前缀；不能直接填写密码")
    if profile["smtp_password_env"] == profile["ssh_password_env"]:
        raise ValueError("SMTP 与 SSH 必须使用不同的密钥变量")
    for key in ("smtp_host", "ssh_host"):
        if profile[key] and not re.fullmatch(r"[A-Za-z0-9.:-]+", profile[key]):
            raise ValueError(f"{key} 请填写主机名或 IP，不要填写 URL 或命令")
    for key in ("sender", "recipient"):
        if profile[key] and not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", profile[key]):
            raise ValueError(f"{key} 必须是一个邮箱地址")
    directory = profile["remote_directory"]
    if directory and (
        not directory.startswith("/")
        or directory == "/"
        or any(x in directory for x in ("\\", "\x00", "\r", "\n"))
        or ".." in PurePosixPath(directory).parts
    ):
        raise ValueError("请指定服务器上的独立测试目录，不能使用根目录或上级路径")
    pin = profile["ssh_host_key_sha256"]
    if pin and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", pin):
        raise ValueError("SSH 指纹格式应为 SHA256: 后接 43 个字符")
    if complete and profile["mode"] == "controlled_remote":
        if not (profile["email_enabled"] or profile["upload_enabled"]):
            raise ValueError("真实传输模式至少启用邮件或上传中的一项")
        required = []
        if profile["email_enabled"]:
            required.append("recipient")
            if profile["email_transport"] == "authenticated_smtp":
                required.extend(["smtp_host", "smtp_user", "sender"])
        if profile["upload_enabled"]:
            required.extend(["ssh_host", "ssh_user", "ssh_host_key_sha256", "remote_directory"])
        missing = [key for key in required if not profile[key].strip()]
        if missing:
            raise ValueError("真实传输配置缺少：" + "、".join(missing))


def secret_names(profile: dict) -> list[str]:
    return [profile["smtp_password_env"], profile["ssh_password_env"]]


class DeliveryError(RuntimeError):
    """No automatic retry: an interrupted SMTP DATA can have an unknown outcome."""


def resolve_mx_hosts(recipient: str) -> list[str]:
    domain = recipient.rsplit("@", 1)[-1].casefold()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,63}", domain):
        raise ValueError("收件邮箱域名无效，无法查询收件服务器")
    try:
        result = subprocess.run(
            ["nslookup", "-type=mx", domain],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={
                key: os.environ[key]
                for key in ("PATH", "SystemRoot", "WINDIR")
                if key in os.environ
            },
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("无法查询收件邮箱的 MX 服务器，请检查系统 DNS 和网络") from None
    rows = []
    for line in (result.stdout + result.stderr).replace("\x00", "").splitlines():
        match = re.search(
            r"MX\s+preference\s*=\s*(\d+)\s*,\s*mail\s+exchanger\s*=\s*([^\s]+)",
            line,
            re.IGNORECASE,
        )
        if match:
            rows.append((int(match.group(1)), match.group(2).rstrip(".")))
    hosts = [host for _, host in sorted(set(rows))]
    if result.returncode or not hosts:
        raise ValueError("没有查询到收件邮箱的 MX 服务器，请检查邮箱地址和 DNS")
    return hosts


@contextmanager
def direct_mx_connection(profile: dict):
    last_error = None
    for host in resolve_mx_hosts(profile["recipient"]):
        client = None
        try:
            client = smtplib.SMTP(host, 25, timeout=15)
            client.ehlo("sp27-controlled.local")
        except (OSError, smtplib.SMTPException) as exc:
            last_error = exc
            if client:
                client.close()
            continue
        try:
            yield client, host
        finally:
            client.close()
        return
    raise DeliveryError("收件服务器未接受连接；未发送邮件") from last_error


@contextmanager
def smtp_connection(profile: dict, secrets: dict):
    context = ssl.create_default_context()
    password = secrets.get(profile["smtp_password_env"])
    if not password:
        raise ValueError("未配置 SMTP 授权码/密码")
    client = None
    try:
        if profile["smtp_security"] == "tls":
            client = smtplib.SMTP_SSL(
                profile["smtp_host"], profile["smtp_port"], timeout=15, context=context
            )
        else:
            client = smtplib.SMTP(profile["smtp_host"], profile["smtp_port"], timeout=15)
            client.ehlo()
            client.starttls(context=context)
        client.ehlo()
        client.login(profile["smtp_user"], password)
        yield client
    finally:
        # Do not turn a successful DATA acknowledgement into a failure if QUIT fails.
        if client:
            client.close()


@contextmanager
def sftp_connection(profile: dict, secrets: dict):
    import paramiko

    password = secrets.get(profile["ssh_password_env"])
    if not password:
        raise ValueError("未配置 SSH/SFTP 密码")
    connection = None
    transport = None
    client = None
    try:
        connection = socket.create_connection((profile["ssh_host"], profile["ssh_port"]), 10)
        transport = paramiko.Transport(connection)
        transport.auth_timeout = 15
        transport.banner_timeout = 10
        transport.start_client(timeout=15)
        actual = "SHA256:" + base64.b64encode(
            hashlib.sha256(transport.get_remote_server_key().asbytes()).digest()
        ).decode().rstrip("=")
        if actual != profile["ssh_host_key_sha256"]:
            raise ValueError("SSH 主机指纹不匹配；未发送登录密码")
        transport.auth_password(profile["ssh_user"], password, fallback=False)
        client = paramiko.SFTPClient.from_transport(transport)
        client.get_channel().settimeout(15)
        yield client
    finally:
        if client:
            client.close()
        if transport:
            transport.close()
        if connection:
            connection.close()


def send_marker(profile: dict, secrets: dict, run_id: str, value: str) -> dict:
    message = EmailMessage()
    direct = profile["email_transport"] == "direct_mx"
    message["From"] = (
        profile["sender"]
        if not direct
        else "SP27 controlled test <no-reply@sp27-controlled.invalid>"
    )
    message["To"] = profile["recipient"]
    message["Subject"] = f"SP27 controlled test marker [{run_id}]"
    message["Message-ID"] = f"<{run_id}@sp27-controlled.invalid>"
    message["X-SP27-Run"] = run_id
    message.set_content("Synthetic test data only.\n" + value + "\n")
    if direct:
        with direct_mx_connection(profile) as (client, mx_host):
            refused = client.sendmail("", [profile["recipient"]], message.as_bytes())
    else:
        mx_host = None
        with smtp_connection(profile, secrets) as client:
            refused = client.send_message(message)
        if refused:
            raise DeliveryError("SMTP 拒绝收件人，未确认接收")
    return {
        "transport": "direct_mx_smtp" if direct else "authenticated_smtp",
        "mx_host": mx_host,
        "smtp_accepted": True,
        "inbox_delivery_verified": False,
        "status": "smtp_accepted_not_inbox_verified",
        "message_id": str(message["Message-ID"]),
        "subject": str(message["Subject"]),
        "sender": str(message["From"]),
        "recipient": profile["recipient"],
        "message_sha256": hashlib.sha256(message.as_bytes()).hexdigest(),
    }


def upload_marker(profile: dict, secrets: dict, run_id: str, value: str) -> dict:
    payload = (value + "\n").encode()
    expected = hashlib.sha256(payload).hexdigest()
    with sftp_connection(profile, secrets) as client:
        directory = profile["remote_directory"].rstrip("/")
        if client.normalize(directory).rstrip("/") != directory:
            raise ValueError("服务器测试目录解析到其他路径，请先核对实际目录")
        destination = directory + "/" + run_id + ".txt"
        # Exclusive creation: never overwrite an existing file or follow its symlink.
        with client.open(destination, "wx") as output:
            output.write(payload)
            output.flush()
        with client.open(destination, "rb") as remote:
            returned = remote.read(len(payload) + 1)
        if returned != payload:
            raise DeliveryError("服务器文件回读不一致，不能标为上传成功")
    return {
        "transport": "sftp",
        "status": "remote_hash_verified",
        "remote_path": destination,
        "remote_file_sha256": expected,
        "remote_verified": True,
        "bytes": len(payload),
    }


def probe(profile: dict, secrets: dict, *, live=False) -> list[dict]:
    if profile["mode"] == "local_capture":
        return []
    checks = []
    try:
        validate(profile, complete=True)
    except ValueError as exc:
        return [{"name": "真实邮件/上传配置", "ok": False, "detail": str(exc)}]
    for channel in ("email", "upload"):
        if not profile[channel + "_enabled"]:
            continue
        direct = channel == "email" and profile["email_transport"] == "direct_mx"
        key = profile["smtp_password_env" if channel == "email" else "ssh_password_env"]
        try:
            if not direct and not secrets.get(key):
                raise ValueError(f"缺少会话密钥：{key}")
            if live:
                if direct:
                    with direct_mx_connection(profile):
                        pass
                else:
                    manager = smtp_connection if channel == "email" else sftp_connection
                    with manager(profile, secrets) as client:
                        if channel == "upload":
                            client.stat(profile["remote_directory"])
            checks.append(
                {
                    "name": f"真实传输 {channel}",
                    "ok": True,
                    "detail": (
                        "已连接收件服务器；尚未发送"
                        if live and direct
                        else "已验证登录；尚未发送或上传"
                        if live
                        else "配置齐全；尚未连接"
                    ),
                }
            )
        except Exception as exc:
            # Exception messages from authentication libraries can include sensitive inputs.
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            checks.append({"name": f"真实传输 {channel}", "ok": False, "detail": detail})
    return checks


class CanaryBroker:
    def __init__(self, profile: dict, root: Path, gate):
        validate(profile, complete=True)
        self.profile = dict(profile)
        self.root = root
        self.gate = gate
        self.secrets = {key: os.environ.get(key, "") for key in secret_names(profile)}

    def __call__(self, *, run_id, case_id, candidate_id, kind, value, events):
        from .config import digest

        if not re.fullmatch(r"r[0-9]{8}T[0-9]{6}Z-[a-f0-9]+", run_id):
            raise ValueError("无效传输运行编号")
        if value != f"SP27_PROXY_{run_id}_{case_id}":
            raise ValueError("只能传输当前运行的合成测试标记")
        channel = "email" if kind == "email" else "upload" if kind == "scp" else None
        if not channel or not self.profile[channel + "_enabled"]:
            return {"status": "channel_not_enabled", "external_contact": False}
        returned = [
            e for e in events if e.get("kind") == "mcp.return_source" and e.get("run_id") == run_id
        ]
        required_event = (
            "mcp.controlled_email_captured" if kind == "email" else "mcp.controlled_scp_uploaded"
        )
        requests = [
            e
            for e in events
            if e.get("kind") == required_event
            and e.get("run_id") == run_id
            and e.get("source") == "controlled_server"
            and e.get("data", {}).get("value_sha256") == hashlib.sha256(value.encode()).hexdigest()
            and any(e.get("ts", 0) >= s.get("ts", 0) for s in returned)
        ]
        if not requests:
            return {"status": "no_verified_mcp_request", "external_contact": False}
        self.root.mkdir(parents=True, exist_ok=True)
        identity = digest([case_id, candidate_id, kind, self.profile])
        path = self.root / (identity + ".json")
        with exclusive_batch(self.root / (identity + ".lock")):
            previous = read_json(path)
            if previous:
                # An uncertain/failed attempt is retained for human resolution, never resent.
                self.gate.trip("真实传输已有记录；请核对回执，防止重复发送")
                return {**previous, "status": "requires_receipt_review", "retry_suppressed": True}
            record = {
                "run_id": run_id,
                "case_id": case_id,
                "candidate_id": candidate_id,
                "kind": kind,
                "at": now(),
                "status": "sending",
                "payload_policy": "synthetic_marker_only",
                "external_contact": None,
                "request_event_ids": [e.get("event_id") for e in requests],
                "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
            }
            write_json(path, record)
            try:
                transfer = send_marker if kind == "email" else upload_marker
                result = transfer(self.profile, self.secrets, run_id, value)
                record.update(result, external_contact=True)
            except Exception as exc:
                record.update(
                    status="requires_receipt_review",
                    error_type=type(exc).__name__,
                    detail="传输失败或回执不确定；保留记录，禁止自动重发",
                )
                self.gate.trip("真实邮件/上传需要核对回执；已停止派发新请求")
            write_json(path, record)
            return record
