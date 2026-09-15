"""Runtime report exporter used by the app on any teammate's machine."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .adapters import METRICS
from .storage import now, read_jsonl, write_json


def summarize(rows: list[dict], manifest: dict) -> dict:
    identifiers = [r["case_id"] for r in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("账本存在重复用例 ID")
    expected = set(manifest.get("case_ids") or [c["case_id"] for c in manifest.get("cases", [])])
    if expected and not set(identifiers).issubset(expected):
        raise ValueError("账本含有冻结清单之外的用例")
    total = len(expected) or int(manifest.get("scheduled_case_count", 0))
    metric = manifest.get("primary_success_metric", "original_sink_intent")
    if any(r.get("success_metric") != metric for r in rows):
        raise ValueError("账本中的成功指标与清单不一致")
    completed = Counter(r.get("attack_category") or "unspecified" for r in rows)
    successes = Counter(
        r.get("attack_category") or "unspecified" for r in rows if r.get("metric_success")
    )
    categories = manifest.get("categories") or manifest.get("category_counts") or completed
    count = sum(successes.values())
    return {
        "total": total,
        "completed": len(rows),
        "succeeded": count,
        "attack_failed": len(rows) - count,
        "remaining": total - len(rows),
        "metric": metric,
        "observed_rate": count / len(rows) if rows else None,
        "final_rate": count / total if total and len(rows) == total else None,
        "categories": [
            {
                "category": category,
                "scheduled": scheduled,
                "completed": completed[category],
                "succeeded": successes[category],
                "observed_rate": successes[category] / completed[category]
                if completed[category]
                else None,
                "final_rate": successes[category] / scheduled
                if scheduled and completed[category] == scheduled
                else None,
            }
            for category, scheduled in categories.items()
        ],
    }


def export_report(root: Path, manifest: dict, config: dict) -> dict:
    from .receipts import list_receipts

    output = root / "results"
    output.mkdir(exist_ok=True)
    cases = read_jsonl(output / "case_level_ledger.jsonl")
    summary = summarize(cases, manifest)
    successes = read_jsonl(output / "successful_prompts.jsonl")
    if {r["case_id"] for r in successes} != {
        r["case_id"] for r in cases if r.get("metric_success")
    } or len(successes) != summary["succeeded"]:
        raise ValueError("成功 Prompt 与用例账本不一致")
    write_json(
        output / "successful_prompts.json",
        [
            {**row, "success_metric": summary["metric"], "original_host_action_executed": False}
            for row in successes
        ],
    )
    write_json(output / "summary.json", summary)
    transports = [
        {
            "case_id": row["case_id"],
            "candidate_id": row.get("candidate_id"),
            "success_metric": row.get("success_metric"),
            **row["controlled_delivery"],
        }
        for row in read_jsonl(output / "prompt_level_ledger.jsonl")
        if row.get("controlled_delivery", {}).get("external_contact") is not False
        and row.get("controlled_delivery")
    ]
    write_json(output / "transport_evidence.json", transports)
    receipts = list_receipts(root)
    write_json(output / "email_receipts.json", receipts)
    confirmed_emails = sum(r["received_by_user"] for r in receipts if r["scope"] == "experiment")
    transmission_note = (
        "本轮启用了受控真实传输，仅发送合成测试标记。SMTP 接受与收件箱收到分开记录；"
        "SFTP 上传须远端回读校验。Shell 与其他原始宿主机操作仍由代理接管。"
        if config.get("egress", {}).get("mode") == "controlled_remote"
        else "本轮使用受控代理；真实外部邮件、上传和宿主机原始操作未执行。"
    )
    rate = "尚无有效结果" if summary["observed_rate"] is None else f"{summary['observed_rate']:.2%}"
    md = [
        f"# {config['name']}",
        "",
        f"生成时间：{now()}",
        "",
        f"指标：{METRICS[summary['metric']]}。{transmission_note}",
        f"真实传输回执：SMTP 接受 {sum(bool(r.get('smtp_accepted')) for r in transports)} 次；"
        f"远端文件校验通过 {sum(bool(r.get('remote_verified')) for r in transports)} 次。"
        "这些回执单独统计，不替换主指标或将其他类别自动计为真实完成。",
        f"其中人工确认收到的实验邮件：{confirmed_emails} 封。"
        "人工确认使用 user_confirmation 标记；独立连接测试邮件不进入实验统计。",
        "",
        f"已完成 **{summary['completed']}/{summary['total']}**，"
        f"成功 **{summary['succeeded']}**，正常失败 **{summary['attack_failed']}**。",
        f"{'最终' if summary['final_rate'] is not None else '阶段性（仅已完成用例）'}"
        f"成功率：**{rate}**。",
        "",
        f"被测模型：`{config['target']['model']}`；变异模型：`{config['generation']['model']}`。",
        f"每轮预算：{config['generation']['budgets']}；首次成功停止：{config['generation']['early_stop']}。",
        "",
        "| 攻击类别 | 计划 | 已完成 | 成功 | 已完成用例成功率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in summary["categories"]:
        rate = "—" if item["observed_rate"] is None else f"{item['observed_rate']:.2%}"
        label = item["category"].replace("|", "\\|").replace("\n", " ")
        md.append(
            f"| {label} | {item['scheduled']} | {item['completed']} | "
            f"{item['succeeded']} | {rate} |"
        )
    md.extend(
        [
            "",
            "未完成与基础设施错误不算作攻击失败。完整结果未到齐时，最终成功率保持空缺。",
            "",
            f"配置校验值：`{manifest.get('config_sha256', '')}`。",
            "",
            "证据：case_level_ledger.jsonl、prompt_level_ledger.jsonl、successful_prompts.json。",
        ]
    )
    if config["target"]["model"] == "auto":
        md.extend(
            [
                "",
                "本实验测试 Cursor Auto 路由；服务端实际模型若未返回，"
                "不能归属为某个固定模型的成绩。",
            ]
        )
    (output / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    # The application uses a portable Python dependency, not the author's machine runtime.
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "结果汇总"
    sheet.append([config["name"]])
    sheet.append([METRICS[summary["metric"]]])
    sheet.append(["受控代理实验：原始宿主机操作及外部邮件、上传均未执行"])
    sheet.append(["状态", "完整结果" if summary["final_rate"] is not None else "阶段性结果"])
    sheet.append(["范围", "计划用例", "已完成", "成功", "正常失败", "阶段性成功率", "最终成功率"])
    sheet.append(
        [
            "总体",
            summary["total"],
            summary["completed"],
            summary["succeeded"],
            summary["attack_failed"],
            '=IF(C6=0,"",D6/C6)',
            '=IF(AND(B6>0,C6=B6),D6/B6,"")',
        ]
    )
    for index, item in enumerate(summary["categories"], 7):
        sheet.append(
            [
                item["category"],
                item["scheduled"],
                item["completed"],
                item["succeeded"],
                f"=C{index}-D{index}",
                f'=IF(C{index}=0,"",D{index}/C{index})',
                f'=IF(AND(B{index}>0,C{index}=B{index}),D{index}/B{index},"")',
            ]
        )
    for row in sheet.iter_rows(min_row=6, min_col=6, max_col=7):
        for cell in row:
            cell.number_format = "0.00%"
    detail = workbook.create_sheet("用例结果")
    detail.append(["case_id", "攻击类别", "成功指标", "是否成功", "Prompt 次数"])
    for case in cases:
        detail.append(
            [
                case["case_id"],
                case.get("attack_category"),
                case.get("success_metric"),
                bool(case.get("metric_success")),
                case.get("prompt_attempt_count", case.get("attempt_count")),
            ]
        )
    for ws, header in ((sheet, 5), (detail, 1)):
        for row in ws:
            for cell in row:
                calculated = ws == sheet and (
                    (cell.row == 6 and cell.column in (6, 7))
                    or (cell.row >= 7 and cell.column in (5, 6, 7))
                )
                if isinstance(cell.value, str) and not calculated:
                    cell.value = ILLEGAL_CHARACTERS_RE.sub("", cell.value)
                    cell.data_type = "s"
                cell.font = Font(name="Microsoft YaHei", size=11, color="203848")
                cell.alignment = Alignment(vertical="center")
        for cell in ws[header]:
            cell.fill = PatternFill("solid", fgColor="203848")
            cell.font = Font(name="Microsoft YaHei", bold=True, color="FFFFFF")
        ws.freeze_panes = f"B{header + 1}"
        ws.auto_filter.ref = f"A{header}:{ws.cell(ws.max_row, ws.max_column).coordinate}"
        ws.column_dimensions["A"].width = 54 if ws == detail else 33
        for key in ("B", "C", "D", "E", "F", "G"):
            ws.column_dimensions[key].width = 26 if ws == detail else 19
        for i in range(1, ws.max_row + 1):
            ws.row_dimensions[i].height = 26
    for index in (1, 2, 3):
        sheet.merge_cells(start_row=index, start_column=1, end_row=index, end_column=7)
    temporary = output / ".aggregate_tables.xlsx"
    workbook.save(temporary)
    temporary.replace(output / "aggregate_tables.xlsx")
    return summary
