from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def _find_project_root() -> Path:
    configured = os.environ.get("CURSOR_EVAL_PROJECT_ROOT")
    if configured:
        return Path(configured).resolve()
    candidates = (PACKAGE_ROOT.parent.parent, Path.cwd().resolve())
    for candidate in candidates:
        if (candidate / "config" / "chains_runtime.json").is_file():
            return candidate
    return PACKAGE_ROOT.parent.parent


PROJECT_ROOT = _find_project_root()
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
RUNTIME_DIR = PROJECT_ROOT / "runtime"

CHAINS_CONFIG = CONFIG_DIR / "chains_runtime.json"
EVALUATOR_CONFIG = CONFIG_DIR / "evaluator.json"
APPROVAL_CONFIG = CONFIG_DIR / "approval_policy.json"
CHAIN_AUTHORIZATION_CONFIG = CONFIG_DIR / "chain_authorization_profiles.json"


def ensure_runtime_dirs() -> None:
    (OUTPUTS_DIR / "runs").mkdir(parents=True, exist_ok=True)
    (OUTPUTS_DIR / "screenshots").mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
