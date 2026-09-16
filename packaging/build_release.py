"""Build a Windows release, including a sanitized public package."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cursor_dynamic_eval.workbench import VERSION  # noqa: E402
from cursor_dynamic_eval.workbench.config import engine_fingerprint  # noqa: E402


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--public",
        action="store_true",
        help="Bundle only the synthetic example corpus and use the public product name.",
    )
    return parser.parse_args()


def main():
    options = _arguments()
    package_name = "AgenticIPI-Workbench" if options.public else "ExperimentWorkbench"
    executable_name = "AgenticIPIWorkbench" if options.public else "ExperimentWorkbench"
    corpus_name = (
        "workbench_example.json" if options.public else "teacher_new_windows_full.json"
    )
    stage = ROOT / "build" / (
        "release-assets-public" if options.public else "release-assets"
    )
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "engine-fingerprint.txt").write_text(engine_fingerprint(), encoding="utf-8")
    target = ROOT / "dist" / f"{package_name}-v{VERSION}"
    config_files = [
        "chains_runtime.json",
        "evaluator.json",
        "approval_policy.json",
        "chain_authorization_profiles.json",
    ]
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        executable_name,
        "--distpath",
        str(target),
        "--workpath",
        str(ROOT / "build" / ("pyinstaller-public" if options.public else "pyinstaller")),
        "--specpath",
        str(ROOT / "build"),
        "--paths",
        str(ROOT / "src"),
        "--collect-submodules",
        "cursor_dynamic_eval.workbench",
        "--hidden-import",
        "cursor_dynamic_eval.mcp_server.controlled_server",
        "--hidden-import",
        "openpyxl",
        "--hidden-import",
        "psutil",
        "--collect-submodules",
        "paramiko",
        "--collect-all",
        "webview",
        "--hidden-import",
        "webview.platforms.edgechromium",
        "--hidden-import",
        "clr",
        "--exclude-module",
        "playwright",
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "matplotlib",
        "--add-data",
        f"{ROOT / 'src/cursor_dynamic_eval/workbench/static'};cursor_dynamic_eval/workbench/static",
        "--add-data",
        f"{stage / 'engine-fingerprint.txt'};cursor_dynamic_eval/workbench",
        "--add-data",
        f"{ROOT / 'config/corpora' / corpus_name};project/config/corpora",
    ]
    for name in config_files:
        args.extend(["--add-data", f"{ROOT / 'config' / name};project/config"])
    for plugin in importlib.metadata.entry_points(group="cursor_dynamic_eval.adapters"):
        args.extend(["--collect-submodules", plugin.value.split(":", 1)[0].split(".", 1)[0]])
        if plugin.dist:
            args.extend(["--copy-metadata", plugin.dist.metadata["Name"]])
    args.append(str(ROOT / "packaging" / "desktop_entry.py"))
    subprocess.run(args, cwd=ROOT, check=True)
    executable = target / f"{executable_name}.exe"
    print(
        json.dumps(
            {
                "exe": str(executable),
                "exe_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "public": options.public,
                "bundled_corpus": corpus_name,
                "shell": "native-webview2",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
