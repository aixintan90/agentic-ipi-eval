from __future__ import annotations

from pathlib import Path

import pytest

from cursor_dynamic_eval.config import ChainSpec, load_chain_specs
from cursor_dynamic_eval.core.run_context import RunContext

RUN_ID = "r20260729T100000Z-deadbeef"


@pytest.fixture()
def specs() -> dict[str, ChainSpec]:
    return load_chain_specs()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "agent_workspace"
    root.mkdir()
    return root


@pytest.fixture()
def make_context(workspace: Path, specs: dict[str, ChainSpec]):
    def factory(chain_id: str = "CHAIN-03", injection: str = "on") -> RunContext:
        return RunContext.create(
            specs[chain_id],
            prompt_id=f"TEST-{chain_id}",
            prompt_condition="P1",
            injection=injection,
            workspace=workspace,
            run_id=RUN_ID,
            http_port=18999,
            started_at=1000.0,
        )

    return factory
