"""Read-only workbook import with explicit provenance and no guessed execution mappings."""

import base64
import copy
import hashlib
import io
import json
import zipfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from openpyxl import load_workbook

from ..automation.corpus import _case_from_dict


def preview(files: list, project: Path) -> tuple[dict, dict]:
    if not isinstance(files, list) or not 1 <= len(files) <= 100:
        raise ValueError("请选择 1–100 个用例文件")
    reference_path = project / "config/corpora/teacher_new_windows_full.json"
    reference = (
        json.loads(reference_path.read_text(encoding="utf-8")) if reference_path.is_file() else {}
    )
    known_files = {
        value: name
        for name, value in reference.get("source", {}).get("workbook_sha256", {}).items()
    }
    reference_rows = {
        (
            c["metadata"]["source_workbook"],
            c["metadata"]["source_sheet"],
            c["metadata"]["source_row"],
        ): c
        for c in reference.get("cases", [])
    }
    cases, problems, sheets, sources = [], [], [], []
    source_total, total_bytes = 0, 0
    for uploaded in files:
        name = Path(str(uploaded["filename"]).replace("\\", "/")).name
        try:
            content = base64.b64decode(uploaded["base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("文件编码无效") from exc
        total_bytes += len(content)
        if total_bytes > 20_000_000:
            raise ValueError("本次导入总大小不能超过 20 MB")
        sha = hashlib.sha256(content).hexdigest()
        sources.append({"name": name, "sha256": sha, "bytes": len(content)})
        suffix = Path(name).suffix.lower()
        if suffix in {".json", ".jsonl"}:
            parsed = (
                [
                    json.loads(line)
                    for line in content.decode("utf-8-sig").splitlines()
                    if line.strip()
                ]
                if suffix == ".jsonl"
                else json.loads(content.decode("utf-8-sig"))
            )
            rows = parsed.get("cases") if isinstance(parsed, dict) else parsed
            if not isinstance(rows, list):
                problems.append(f"{name}：JSON 必须包含 cases 数组")
                continue
            source_total += len(rows)
            for index, row in enumerate(rows, 1):
                try:
                    cases.append(asdict(_case_from_dict(row, index=index)))
                except (ValueError, TypeError, AttributeError) as exc:
                    problems.append(f"{name} 第 {index} 条：{exc}")
            sheets.append({"file": name, "sheet": "JSON", "rows": len(rows), "mode": "标准字段"})
            continue
        if suffix != ".xlsx":
            problems.append(f"{name}：只支持 .xlsx、.json 或 .jsonl")
            continue
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 64_000_000:
                raise ValueError(f"{name} 解压大小超过 64 MB")
        workbook = load_workbook(
            io.BytesIO(content), read_only=True, data_only=False, keep_links=False
        )
        try:
            for sheet in workbook.worksheets:
                if (
                    sheet.max_row
                    and sheet.max_row > 10001
                    or sheet.max_column
                    and sheet.max_column > 128
                ):
                    problems.append(f"{name}/{sheet.title}：超过 10000 行或 128 列限制")
                    continue
                iterator = sheet.iter_rows(values_only=True)
                headers = [str(value or "").strip() for value in next(iterator, ())]
                teacher = "ID" in headers and "Atomic Seed Instruction" in headers
                standard = {"case_id", "chain_id", "user_prompt", "tool_response_on"}.issubset(
                    headers
                )
                if not teacher and not standard:
                    # Dictionary/coverage sheets are not cases; report that explicitly.
                    if sheet.title in {"Data Dictionary", "Coverage Summary"}:
                        sheets.append(
                            {
                                "file": name,
                                "sheet": sheet.title,
                                "rows": 0,
                                "mode": "辅助说明表（不计为用例）",
                            }
                        )
                    elif any(any(v is not None for v in row) for row in iterator):
                        problems.append(f"{name}/{sheet.title}：无法识别列名，未静默跳过")
                    continue
                active_headers = [h for h in headers if h]
                if len(set(active_headers)) != len(active_headers):
                    problems.append(f"{name}/{sheet.title}：存在重复列名")
                    continue
                count = 0
                mapping = known_files.get(sha) if teacher else None
                for row_number, values in enumerate(iterator, 2):
                    if not any(v is not None and str(v).strip() for v in values):
                        continue
                    count += 1
                    row = {header: values[i] for i, header in enumerate(headers) if header}
                    if teacher:
                        original = reference_rows.get((mapping, sheet.title, row_number))
                        if original:
                            case = copy.deepcopy(original)
                        else:
                            continue
                    else:
                        required = ("case_id", "chain_id", "user_prompt", "tool_response_on")
                        if any(
                            isinstance(row.get(k), str) and row[k].startswith("=") for k in required
                        ):
                            problems.append(
                                f"{name}/{sheet.title} 第 {row_number} 行：必要字段不能是公式"
                            )
                            continue
                        try:
                            # Blank required identifiers must not silently become generated IDs.
                            if any(not str(row.get(k) or "").strip() for k in required):
                                raise ValueError("缺少用例编号、调用链、合法任务或注入内容")
                            case = asdict(_case_from_dict(row, index=row_number))
                        except (ValueError, TypeError) as exc:
                            problems.append(f"{name}/{sheet.title} 第 {row_number} 行：{exc}")
                            continue
                    case["metadata"]["import_source"] = {
                        "workbook": name,
                        "sheet": sheet.title,
                        "row": row_number,
                        "workbook_sha256": sha,
                    }
                    cases.append(case)
                source_total += count
                sheets.append(
                    {
                        "file": name,
                        "sheet": sheet.title,
                        "rows": count,
                        "mode": "已核验的老师原表映射"
                        if mapping
                        else "标准字段"
                        if standard
                        else "缺少执行映射",
                    }
                )
                if teacher and not mapping:
                    problems.append(
                        f"{name}/{sheet.title}：{count} 条原表用例没有匹配的审核映射。"
                        "新版或修改后的原表需提供 chain_id、user_prompt、tool_response_on "
                        "等明确字段，不能套用旧映射。"
                    )
        finally:
            workbook.close()
    duplicate_ids = [
        key for key, count in Counter(c["case_id"] for c in cases).items() if count > 1
    ]
    if duplicate_ids:
        problems.append("重复用例 ID：" + "、".join(duplicate_ids[:15]))
    if source_total != len(cases):
        problems.append(
            f"源用例 {source_total} 条，可解析 {len(cases)} 条；未完成映射前不能确认导入"
        )
    if not cases:
        problems.append("没有可导入用例")
    categories = dict(Counter(c["metadata"].get("attack_category", "unspecified") for c in cases))
    payload = {"schema_version": "workbench-corpus/1.0", "sources": sources, "cases": cases}
    result = {
        "case_count": len(cases),
        "source_case_count": source_total,
        "categories": categories,
        "sheets": sheets,
        "problems": problems,
        "can_import": not problems,
        "sample": [
            {
                "case_id": c["case_id"],
                "chain_id": c["chain_id"],
                "category": c["metadata"].get("attack_category", "unspecified"),
                "source": c["metadata"].get("import_source", {}),
            }
            for c in cases[:20]
        ],
    }
    return payload, result
