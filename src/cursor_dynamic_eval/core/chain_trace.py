"""Version-bound, chain-specific dynamic trace judgment.

The A--F judge answers whether an experiment has a complete provenance/effect
evidence chain.  This module separately answers how far a *specific Cursor
execution path* progressed.  Static contract nodes are deliberately not
counted as runtime observations.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import PROJECT_ROOT
from .events import Event, sort_events
from .run_context import RunContext

STATIC_NODE_IDS = {"registry", "schema"}
RUNTIME_EVENT_KIND = "inspector.node_hit"


@dataclass(frozen=True)
class ContractNode:
    node_id: str
    offset: int
    needle: str
    role: str
    alternative_anchors: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class ChainContract:
    chain_id: str
    name: str
    argument_shape: str
    oracle: str
    nodes: tuple[ContractNode, ...]

    @property
    def runtime_nodes(self) -> tuple[ContractNode, ...]:
        return tuple(node for node in self.nodes if node.node_id not in STATIC_NODE_IDS)


@dataclass(frozen=True)
class ChainContractCatalog:
    cursor_version: str
    bundle_sha256: str
    bundle_size_bytes: int
    chains: dict[str, ChainContract]
    path: Path

    def contract_for(self, chain_id: str) -> ChainContract | None:
        return self.chains.get(chain_id)


def call_chain_progress(trace: dict[str, Any] | None) -> dict[str, Any]:
    """Return a presentation-ready view of a version-bound runtime contract.

    The A--F provenance levels are intentionally not used here.  This value is
    the only call-chain progress shown to researchers: it names the concrete
    contract steps and reports how many runtime steps were actually observed.
    """

    value = dict(trace or {})
    nodes = [
        {
            "id": str(node.get("id") or ""),
            "role": str(node.get("role") or ""),
            "status": str(node.get("status") or "unobserved"),
        }
        for node in value.get("nodes") or []
        if isinstance(node, dict) and not bool(node.get("static"))
    ]
    return {
        "chain_id": str(value.get("chain_id") or ""),
        "chain_name": str(value.get("chain_name") or ""),
        "status": str(value.get("status") or "unavailable"),
        "completed_steps": int(value.get("reached_count") or 0),
        "total_steps": int(value.get("runtime_node_count") or len(nodes)),
        "last_observed_step": value.get("runtime_depth"),
        "blocked_at_step": value.get("runtime_fail_point"),
        "steps": nodes,
    }


def default_contract_path() -> Path:
    """Return the newest locally validated static-analysis baseline."""
    root = PROJECT_ROOT.parent / "cursor_callchain_analysis"
    candidates = sorted(root.glob("rebaseline_*/contracts.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no Cursor chain contracts under {root}")
    return candidates[0]


def cursor_agent_bundle_path(cursor_executable: Path) -> Path:
    return (
        cursor_executable.resolve().parent
        / "resources"
        / "app"
        / "extensions"
        / "cursor-agent-exec"
        / "dist"
        / "main.js"
    )


def _bundle_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def load_compatible_chain_contract_catalog(
    bundle_path: Path,
    *,
    baseline_root: Path | None = None,
) -> ChainContractCatalog:
    bundle = bundle_path.resolve()
    if not bundle.is_file():
        raise ValueError(f"Cursor agent bundle does not exist: {bundle}")
    actual_hash = _bundle_sha256(bundle)
    actual_size = bundle.stat().st_size
    root = baseline_root or PROJECT_ROOT.parent / "cursor_callchain_analysis"
    checked: list[str] = []
    for path in sorted(root.glob("rebaseline_*/contracts.json"), reverse=True):
        catalog = load_chain_contract_catalog(path)
        checked.append(f"{catalog.cursor_version}:{catalog.bundle_sha256[:12]}")
        if (
            catalog.bundle_sha256 == actual_hash
            and catalog.bundle_size_bytes == actual_size
        ):
            return catalog
    raise ValueError(
        "no compatible Cursor call-chain contract for bundle "
        f"sha256={actual_hash}, size={actual_size}; checked {', '.join(checked)}"
    )


def load_chain_contract_catalog(path: Path | None = None) -> ChainContractCatalog:
    source = (path or default_contract_path()).resolve()
    with source.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    target = raw.get("target") or {}
    rows = raw.get("chains")
    if not isinstance(rows, list):
        raise ValueError(f"{source}: chains must be a list")

    chains: dict[str, ChainContract] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{source}: each chain must be an object")
        chain_id = str(row.get("id") or "")
        raw_nodes = row.get("nodes")
        if not chain_id or not isinstance(raw_nodes, list):
            raise ValueError(f"{source}: invalid chain contract")
        nodes = tuple(
            ContractNode(
                node_id=str(node["id"]),
                offset=int(node["offset"]),
                needle=str(node["needle"]),
                role=str(node["role"]),
                alternative_anchors=tuple(
                    (int(anchor["offset"]), str(anchor["needle"]))
                    for anchor in node.get("alternatives") or []
                    if isinstance(anchor, dict)
                ),
            )
            for node in raw_nodes
            if isinstance(node, dict)
        )
        if not nodes or len({node.node_id for node in nodes}) != len(nodes):
            raise ValueError(f"{source}: {chain_id} has invalid node IDs")
        chains[chain_id] = ChainContract(
            chain_id=chain_id,
            name=str(row.get("name") or chain_id),
            argument_shape=str(row.get("argument_shape") or ""),
            oracle=str(row.get("oracle") or ""),
            nodes=nodes,
        )

    expected = {f"CHAIN-{index:02d}" for index in range(1, 10)}
    if set(chains) != expected:
        raise ValueError(f"{source}: expected CHAIN-01..09")
    bundle_sha256 = str(target.get("sha256") or "").upper()
    if not bundle_sha256:
        raise ValueError(f"{source}: target bundle hash is required")
    return ChainContractCatalog(
        cursor_version=str(target.get("version") or "unknown"),
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=int(target.get("size_bytes") or 0),
        chains=chains,
        path=source,
    )


def _trace_target_matches(event: Event, context: RunContext) -> bool:
    values = [
        event.data.get(key)
        for key in (
            "target",
            "path",
            "url",
            "uri",
            "command",
            "token",
            "marker_run_id",
            "correlation",
        )
    ]
    return any(
        value is not None
        and (
            str(value) == context.injection_target
            or context.run_id in str(value)
        )
        for value in values
    )


def _event_detail(event: Event, node: ContractNode) -> dict[str, Any]:
    return {
        "event_id": event.event_id or "",
        "kind": event.kind,
        "source": event.source or event.surface,
        "node_id": node.node_id,
        "role": node.role,
        "offset": node.offset,
        "tool_call_id": str(event.data.get("tool_call_id") or ""),
        "target": str(
            event.data.get("target")
            or event.data.get("path")
            or event.data.get("url")
            or event.data.get("uri")
            or event.data.get("correlation")
            or ""
        ),
        "timestamp": event.ts,
    }


def judge_chain_trace(
    context: RunContext,
    events: list[Event],
    catalog: ChainContractCatalog | None,
) -> dict[str, Any]:
    """Return a chain-node result without changing the A--F outcome.

    A valid node hit needs all of: the expected chain/node IDs, the version
    hash, and a target/run correlation.  This rejects generic log lines and
    uncorrelated Inspector pauses.
    """
    if catalog is None:
        return {
            "status": "unavailable",
            "reason": "No version-bound chain contract is loaded.",
            "runtime_depth": None,
            "runtime_fail_point": None,
            "reached_count": 0,
            "runtime_node_count": 0,
            "trace_available": False,
            "observed_runtime_events": 0,
            "nodes": [],
            "diagnostics": ["No compatible Cursor contract catalog is available."],
        }
    contract = catalog.contract_for(context.chain_id)
    if contract is None:
        return {
            "status": "unavailable",
            "reason": f"No contract for {context.chain_id}.",
            "runtime_depth": None,
            "runtime_fail_point": None,
            "reached_count": 0,
            "runtime_node_count": 0,
            "trace_available": False,
            "observed_runtime_events": 0,
            "nodes": [],
            "diagnostics": [f"No chain contract for {context.chain_id}."],
        }

    relevant = [
        event
        for event in sort_events(events)
        if event.kind == RUNTIME_EVENT_KIND
        and str(event.data.get("chain_id") or "") == context.chain_id
    ]
    status_events = [
        event
        for event in sort_events(events)
        if event.kind == "inspector.status"
        and str(event.data.get("chain_id") or context.chain_id) == context.chain_id
    ]
    last_status = status_events[-1].data if status_events else {}
    bad_hash = [
        event
        for event in relevant
        if str(event.data.get("bundle_sha256") or "").upper()
        != catalog.bundle_sha256
    ]
    good = [
        event
        for event in relevant
        if str(event.data.get("bundle_sha256") or "").upper()
        == catalog.bundle_sha256
        and _trace_target_matches(event, context)
    ]
    by_node: dict[str, list[Event]] = {}
    for event in good:
        node_id = str(event.data.get("node_id") or "")
        by_node.setdefault(node_id, []).append(event)

    diagnostics: list[str] = []
    if bad_hash:
        diagnostics.append(
            "Inspector events were rejected because their Cursor bundle hash does not "
            "match the loaded Cursor contract."
        )
    if relevant and not good and not bad_hash:
        diagnostics.append(
            "Inspector events were rejected because they lack this run's target/run correlation."
        )
    if not relevant:
        diagnostics.append(
            "No Inspector node trace was collected for this run; static anchors are not "
            "runtime evidence."
        )
    if last_status:
        diagnostics.append(
            "Inspector status: "
            f"{last_status.get('state', 'unknown')}: {last_status.get('detail', '')}"
        )

    nodes: list[dict[str, Any]] = []
    runtime_depth: str | None = None
    runtime_fail_point: str | None = None
    blocked = False
    reached_count = 0
    for node in contract.nodes:
        matching = by_node.get(node.node_id, [])
        is_static = node.node_id in STATIC_NODE_IDS
        if is_static:
            status = "static_contract"
        elif bad_hash:
            status = "unavailable"
        elif any(str(event.data.get("status") or "") == "contradicted" for event in matching):
            status = "contradicted"
        elif matching:
            status = "observed"
        else:
            status = "unobserved"

        if not is_static and not blocked:
            if status == "observed":
                runtime_depth = node.node_id
                reached_count += 1
            else:
                runtime_fail_point = node.node_id
                blocked = True
        nodes.append(
            {
                "id": node.node_id,
                "role": node.role,
                "offset": node.offset,
                "status": status,
                "static": is_static,
                "evidence": [_event_detail(event, node) for event in matching],
            }
        )

    status_state = str(last_status.get("state") or "")
    if bad_hash or status_state == "hash_mismatch":
        trace_status = "hash_mismatch"
    elif status_state in {"error", "unavailable"}:
        trace_status = "unavailable"
    elif good:
        trace_status = "observed"
    else:
        trace_status = "not_observed"
    return {
        "chain_id": contract.chain_id,
        "status": trace_status,
        "cursor_version": catalog.cursor_version,
        "bundle_sha256": catalog.bundle_sha256,
        "contract_path": str(catalog.path),
        "chain_name": contract.name,
        "argument_shape": contract.argument_shape,
        "oracle": contract.oracle,
        "runtime_depth": runtime_depth,
        "runtime_fail_point": runtime_fail_point,
        "reached_count": reached_count,
        "runtime_node_count": len(contract.runtime_nodes),
        "trace_available": bool(good),
        "observed_runtime_events": len(good),
        "nodes": nodes,
        "diagnostics": diagnostics,
        "inspector_status": dict(last_status),
    }
