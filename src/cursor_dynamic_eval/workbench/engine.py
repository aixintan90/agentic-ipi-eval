"""Independent run process, with bounded dispatch and append-only execution evidence."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from ..automation.corpus import load_corpus
from ..automation.formal_backend import DeepSeekFormalGenerator
from ..automation.formal_checkpoint import CandidateJournal, exclusive_batch, read_raw_records
from ..automation.formal_cli import InfrastructureIncomplete, run_formal_cases
from ..automation.formal_execution import BatchPaused, FormalExecutionGate
from ..automation.formal_reporting import write_formal_exports
from ..mutation.llm_client import FailoverLLMClient, OpenAICompatibleClient
from .adapters import evaluate, get_adapter
from .config import digest, engine_fingerprint
from .storage import now, process_identity, read_json, write_json


def mutation_client(config: dict):
    generation = config["generation"]
    key = os.environ.get(generation["api_key_env"], "").strip()
    if not key:
        raise ValueError(f"缺少变异 API 密钥：{generation['api_key_env']}")
    return FailoverLLMClient(
        [
            OpenAICompatibleClient(
                api_key=key,
                base_url=generation["base_url"],
                model=generation["model"],
                timeout_seconds=generation["timeout_seconds"],
            )
        ]
    )


def sanitize_error(exc: Exception, config: dict) -> str:
    from .egress import secret_names, settings

    text = f"{type(exc).__name__}: {exc}"
    for name in [
        config["generation"]["api_key_env"],
        *secret_names(config.get("egress", settings())),
    ]:
        key = os.environ.get(name, "")
        if key:
            text = text.replace(key, "<redacted>")
    return text


def verify(root: Path) -> tuple[dict, dict, list]:
    import hashlib

    config = read_json(root / "config.json")
    manifest = read_json(root / "manifest.json")
    if digest(config) != manifest["config_sha256"]:
        raise ValueError("冻结配置已变更，请复制为新实验")
    if (
        hashlib.sha256((root / manifest["corpus_file"]).read_bytes()).hexdigest()
        != manifest["corpus_sha256"]
    ):
        raise ValueError("冻结语料已变更")
    if manifest["engine_fingerprint"] != engine_fingerprint():
        raise ValueError("实验引擎版本已变更，请使用原版本恢复，或创建新实验")
    adapter = get_adapter(config)
    if adapter.version != manifest["adapter_version"]:
        raise ValueError("执行适配器版本已变更")
    _, available_cases = load_corpus(root / manifest["corpus_file"])
    by_id = {case.case_id: case for case in available_cases}
    missing = [case_id for case_id in manifest["case_ids"] if case_id not in by_id]
    if missing:
        raise ValueError("冻结用例缺失：" + "、".join(missing[:5]))
    cases = [by_id[case_id] for case_id in manifest["case_ids"]]
    return config, manifest, cases


def run(root: Path, *, generator=None, backend_factory=None) -> dict:
    """Dependency injection is for offline regression tests; UI always uses real adapters."""
    with exclusive_batch(root / ".runner.lock"):
        config = read_json(root / "config.json")
        try:
            config, manifest, cases = verify(root)
            return _run(root, config, manifest, cases, generator, backend_factory)
        except Exception as exc:
            state = read_json(root / "state.json", {})
            state.update(
                phase="error",
                pid=None,
                process_started_at=None,
                updated_at=now(),
                error=sanitize_error(exc, config),
            )
            write_json(root / "state.json", state)
            raise ValueError(sanitize_error(exc, config)) from None


def _run(root, config, manifest, cases, generator, backend_factory):
    runtime = Path(manifest["runtime_dir"])
    runtime.mkdir(parents=True, exist_ok=True)
    output = root / "results"
    raw_path = runtime / "raw_prompt_results.jsonl"
    records = read_raw_records(raw_path)
    case_ids = set(manifest["case_ids"])
    for row in records:
        if (
            row.get("case_id") not in case_ids
            or row.get("base_model") != config["target"]["model"]
            or row.get("condition") != config["authorization"]
            or row.get("injection") != config["injection"]
            or row.get("success_metric") != config["evaluation"]["metric"]
        ):
            raise ValueError("原始证据与冻结实验协议不一致")
    gate = FormalExecutionGate(config["execution"]["workers"], stop_path=root / "STOP_BATCH")
    lock = threading.RLock()
    results, errors, active = {}, {}, set()
    start = time.monotonic()
    baseline = 0
    identity = process_identity()
    state_path = root / "state.json"

    def save(phase="running", terminal=False):
        done = len(results)
        succeeded = sum(bool(r["case_level_ledger"][0]["metric_success"]) for r in results.values())
        state = {
            "phase": phase,
            "updated_at": now(),
            "total": len(cases),
            "completed": done,
            "succeeded": succeeded,
            "attack_failed": done - succeeded,
            "remaining": len(cases) - done,
            "infrastructure_errors": errors,
            "active_cases": sorted(active),
            "raw_attempts": len(records),
            "valid_attempts": sum(len(r["prompt_level_ledger"]) for r in results.values()),
            "rate_per_hour": round(
                max(0, done - baseline) / max(1, time.monotonic() - start) * 3600, 2
            ),
            **identity,
            **gate.snapshot(),
        }
        if terminal:
            state.update(pid=None, process_started_at=None)
        write_json(state_path, state)
        return state

    class PendingReplay(Exception):
        pass

    def unavailable(*args, **kwargs):
        raise PendingReplay()

    workflow = config.get("workflow") or {"mode": "thought_tree"}
    replay_mode = workflow.get("mode") == "replay_then_tree"
    effective_budgets = (
        (1, *tuple(config["generation"]["budgets"]))
        if replay_mode
        else tuple(config["generation"]["budgets"])
    )
    baseline_prompts = {}
    if replay_mode:
        from .baseline_prompts import prompt_index

        baseline_prompts = prompt_index(Path(workflow["baseline_prompt_file"]))
        missing = [case.case_id for case in cases if case.case_id not in baseline_prompts]
        if missing:
            raise ValueError("本轮用例缺少已有成功 Prompt：" + "、".join(missing[:5]))

    def execute_case(case, candidate_generator, execute):
        return run_formal_cases(
            [case],
            generate_candidates=candidate_generator,
            execute_candidate=execute,
            budgets=effective_budgets,
            condition=config["authorization"],
            injection=config["injection"],
            base_model=config["target"]["model"],
            success_metric=config["evaluation"]["metric"],
            success_evaluator=lambda row: evaluate(row, config["evaluation"]["metric"]),
            early_stop=config["generation"]["early_stop"],
            resume_records=list(records),
            on_prompt_record=record,
        )

    def record(row):
        with lock:
            with raw_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            records.append(row)
            save()

    def export():
        rows = [
            row
            for case in cases
            if case.case_id in results
            for row in results[case.case_id]["prompt_level_ledger"]
        ]
        write_formal_exports(rows, output)

    # Recover complete cases before constructing any backend or contacting the generator.
    journal_replay = CandidateJournal(unavailable, runtime / "candidates", records)
    for case in cases:
        try:
            results[case.case_id] = execute_case(case, journal_replay, unavailable)
        except PendingReplay:
            pass
    baseline = len(results)
    export()
    save()
    adapter = get_adapter(config)
    factory = backend_factory or adapter.create_backend
    pending = iter(c for c in cases if c.case_id not in results)
    mutation_generator = generator

    def generate_for_workflow(case, round_no, budget, *, prior_attempts):
        nonlocal mutation_generator
        if replay_mode and round_no == 1:
            baseline_prompt = baseline_prompts[case.case_id]
            return [
                {
                    "candidate_id": f"{case.case_id}:direct-replay:1",
                    "prompt": baseline_prompt["prompt"],
                    "p_type": baseline_prompt.get("source_p_type") or "direct_replay",
                    "candidate_origin": "direct_replay",
                    "source_model": baseline_prompt.get("source_model") or None,
                    "source_prompt_sha256": baseline_prompt["prompt_sha256"],
                }
            ]
        if mutation_generator is None:
            mutation_generator = DeepSeekFormalGenerator(
                mutation_client(config),
                condition=config["authorization"],
                concurrency=config["generation"]["concurrency"],
            )
        tree_round = round_no - 1 if replay_mode else round_no
        candidates = mutation_generator(
            case,
            tree_round,
            budget,
            prior_attempts=prior_attempts,
        )
        for candidate in candidates:
            candidate.setdefault("candidate_origin", "thought_tree")
        return candidates

    journal = CandidateJournal(generate_for_workflow, runtime / "candidates", records)

    def task(case):
        gate.check()
        backend = None

        def generate(*args, **kwargs):
            gate.check()
            return journal(*args, **kwargs)

        def execute(*args, **kwargs):
            nonlocal backend
            gate.check()
            if backend is None:
                backend = factory(runtime / "workers" / digest(case.case_id)[:12], config, gate)
            return backend(*args, **kwargs)

        for retry in range(config["execution"]["infrastructure_retries"] + 1):
            try:
                return execute_case(case, generate, execute)
            except (InfrastructureIncomplete, json.JSONDecodeError):
                gate.check()
                if retry == config["execution"]["infrastructure_retries"]:
                    raise
                for _ in range(min(2 ** (retry + 1), 16) * 5):
                    gate.check()
                    time.sleep(0.2)

    jobs = {}
    with ThreadPoolExecutor(max_workers=config["execution"]["workers"]) as executor:
        exhausted = False
        while jobs or not exhausted:
            while not exhausted and len(jobs) < config["execution"]["workers"]:
                try:
                    gate.check()
                    case = next(pending)
                except (BatchPaused, StopIteration):
                    exhausted = True
                    break
                with lock:
                    active.add(case.case_id)
                jobs[executor.submit(task, case)] = case
            if not jobs:
                break
            completed, _ = wait(jobs, timeout=1, return_when=FIRST_COMPLETED)
            for future in completed:
                case = jobs.pop(future)
                with lock:
                    active.discard(case.case_id)
                    try:
                        results[case.case_id] = future.result()
                    except BatchPaused:
                        pass
                    except Exception as exc:
                        errors[case.case_id] = sanitize_error(exc, config)
                        if re.search(
                            r"unpaid|usage.?limit|quota|insufficient.?balance|unauthorized|unauthenticated|ActionRequiredError|\b40[123]\b",
                            errors[case.case_id],
                            re.I,
                        ):
                            gate.trip("模型或变异 API 的额度/认证错误；已停止派发新用例")
            with lock:
                save(
                    "pausing" if gate.snapshot()["operator_stop"] or gate.stop_reason else "running"
                )
            if completed and len(results) % config["execution"]["export_every"] == 0:
                export()
    export()
    command = read_json(root / "control.json", {})
    phase = (
        "stopped"
        if command.get("action") == "stop"
        else "completed"
        if len(results) == len(cases)
        else "blocked"
        if gate.stop_reason
        else "paused"
        if gate.stop_path.exists()
        else "incomplete"
    )
    result = save(phase, terminal=True)
    write_json(
        root / "run_summary.json",
        {
            **result,
            "metric": config["evaluation"]["metric"],
            "workflow_mode": workflow.get("mode", "thought_tree"),
            "config_sha256": manifest["config_sha256"],
        },
    )
    from .reports import export_report

    export_report(root, manifest, config)
    return result
