"""Workbook importer tests: no source edits and no model execution."""

import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from cursor_dynamic_eval.workbench.imports import preview
from cursor_dynamic_eval.workbench.service import WorkbenchService


def encode(name, body):
    return {"filename": name, "base64": base64.b64encode(body).decode()}


def excel(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_standard_excel_import_and_confirmation(tmp_path):
    rows = [
        ["case_id", "chain_id", "user_prompt", "tool_response_on", "attack_category"],
        ["sample-1", "CHAIN-02", "Review.", "Plain context.", "Example"],
        ["sample-2", "CHAIN-02", "Review.", "Another context.", "Example"],
    ]
    service = WorkbenchService(tmp_path)
    result = service.preview_corpus([encode("cases.xlsx", excel(rows))])
    assert result["can_import"] and result["case_count"] == result["source_case_count"] == 2
    imported = service.confirm_corpus(result["token"])
    payload = json.loads(Path(imported["path"]).read_text(encoding="utf-8"))
    assert payload["cases"][0]["metadata"]["import_source"]["row"] == 2
    assert imported["categories"] == {"Example": 2}
    Path(imported["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        service.confirm_corpus(result["token"])


def test_missing_fields_and_duplicate_ids_never_silently_pass(tmp_path):
    rows = [
        ["case_id", "chain_id", "user_prompt", "tool_response_on"],
        ["same-id", "CHAIN-02", "Review.", "Context."],
        ["same-id", "CHAIN-02", "Review.", "Context."],
        [None, "CHAIN-02", "Review.", "Context."],
    ]
    _, result = preview([encode("cases.xlsx", excel(rows))], tmp_path)
    assert not result["can_import"] and result["source_case_count"] == 3
    assert any("重复用例" in p for p in result["problems"])
    assert any("缺少" in p for p in result["problems"])


def test_new_teacher_workbook_requires_reviewed_mapping(tmp_path):
    rows = [["ID", "Atomic Seed Instruction"], ["new-1", "Plain test context."]]
    _, result = preview([encode("teacher.xlsx", excel(rows))], tmp_path)
    assert not result["can_import"] and result["source_case_count"] == 1
    assert result["case_count"] == 0
    assert any("审核映射" in p for p in result["problems"])


def test_teacher_workbook_accepts_separately_uploaded_audit_mapping(tmp_path):
    workbook = excel(
        [["ID", "Atomic Seed Instruction"], ["new-1", "Plain test context."]]
    )
    mapping = {
        "source": {
            "workbook_sha256": {
                "teacher.xlsx": hashlib.sha256(workbook).hexdigest(),
            }
        },
        "cases": [
            {
                "case_id": "mapped-1",
                "chain_id": "CHAIN-02",
                "user_prompt": "Review.",
                "tool_response_on": "Plain test context.",
                "metadata": {
                    "source_workbook": "teacher.xlsx",
                    "source_sheet": "Sheet",
                    "source_row": 2,
                    "attack_category": "Example",
                },
            }
        ],
    }
    payload, result = preview(
        [
            encode("teacher.xlsx", workbook),
            encode("teacher-audit-mapping.json", json.dumps(mapping).encode()),
        ],
        tmp_path,
    )

    assert result["can_import"], result["problems"]
    assert result["case_count"] == result["source_case_count"] == 1
    assert result["mapping"] == {
        "kind": "uploaded",
        "filename": "teacher-audit-mapping.json",
    }
    assert payload["cases"][0]["case_id"] == "mapped-1"
    assert any(sheet["sheet"] == "审核映射" for sheet in result["sheets"])


def test_actual_teacher_workbooks_match_all_1080_without_modification():
    project = Path(__file__).resolve().parents[1]
    source = project.parent / "windows_测试用例/windows"
    if not source.is_dir():
        pytest.skip("Original teacher workbooks are not distributed with the code")
    files = [encode(p.name, p.read_bytes()) for p in sorted(source.glob("*.xlsx"))]
    payload, result = preview(files, project)
    assert result["can_import"], result["problems"]
    assert result["source_case_count"] == result["case_count"] == 1080
    assert len({c["case_id"] for c in payload["cases"]}) == 1080
    assert sum(result["categories"].values()) == 1080
    assert sum(s["mode"].startswith("辅助说明") for s in result["sheets"]) == 2
