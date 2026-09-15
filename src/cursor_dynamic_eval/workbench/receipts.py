"""Operator attestations are separate from immutable machine transport evidence."""

import getpass
import json
import os
import re
from pathlib import Path

from ..automation.formal_checkpoint import exclusive_batch
from .storage import now, read_json, write_json


def list_receipts(root: Path) -> list[dict]:
    manifest = read_json(root / "manifest.json", {})
    paths = list((root / "transport_checks").glob("*.json"))
    runtime = manifest.get("runtime_dir")
    if runtime:
        paths.extend(Path(runtime).glob("workers/*/delivery-evidence/*.json"))
    result = []
    for path in paths:
        row = read_json(path, {})
        if row.get("kind", row.get("channel")) != "email":
            continue
        run_id = str(row.get("run_id", ""))
        if not re.fullmatch(r"r\d{8}T\d{6}Z-[a-f0-9]{8}", run_id):
            continue
        confirmation = read_json(root / "receipt_confirmations" / (run_id + ".json"))
        result.append(
            {
                **row,
                "scope": row.get("scope", "experiment"),
                "confirmation": confirmation,
                "received_by_user": bool(confirmation and confirmation.get("received")),
                "can_confirm": row.get("smtp_accepted") is True,
            }
        )
    return sorted(result, key=lambda row: row.get("at", ""), reverse=True)


def confirm_receipt(root: Path, run_id: str, received: bool, message_id: str) -> dict:
    if type(received) is not bool:
        raise ValueError("必须明确选择收到或撤销确认")
    with exclusive_batch(root / ".receipt.lock"):
        matching = [row for row in list_receipts(root) if row["run_id"] == run_id]
        if len(matching) != 1:
            raise ValueError("找不到唯一的邮件发送记录")
        row = matching[0]
        if not row["can_confirm"] or not message_id or message_id != row.get("message_id"):
            raise ValueError("请核对具体邮件的 Message-ID；SMTP 未明确接受的邮件不能直接标记收到")
        value = {
            "run_id": run_id,
            "message_id": message_id,
            "received": received,
            "confirmed_at": now(),
            "confirmed_by": getpass.getuser(),
            "verification_method": "user_confirmation",
            "scope": row["scope"],
            "statement": "使用者核对收件箱，确认这封测试邮件已收到"
            if received
            else "使用者撤销收到确认",
        }
        folder = root / "receipt_confirmations"
        folder.mkdir(exist_ok=True)
        # Audit every change, including revocation. Never rewrite SMTP evidence or ASR.
        with (folder / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        write_json(folder / (run_id + ".json"), value)
        return value
