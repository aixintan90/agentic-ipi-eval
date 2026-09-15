"""Entry point for the windowless, folder-portable Windows build."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def boot():
    if getattr(sys, "frozen", False):
        project = Path(sys._MEIPASS) / "project"
        if "--project" in sys.argv:
            project = Path(sys.argv[sys.argv.index("--project") + 1]).resolve()
        os.environ["CURSOR_EVAL_PROJECT_ROOT"] = str(project)
        logs = (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ExperimentWorkbench" / "logs"
        )
        logs.mkdir(parents=True, exist_ok=True)
        log_file = logs / "desktop.log"
        if "run" in sys.argv and "--experiment" in sys.argv:
            log_file = Path(sys.argv[sys.argv.index("--experiment") + 1]) / "runner.log"
        if sys.stdout is None:
            sys.stdout = log_file.open("a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = sys.stdout
    from cursor_dynamic_eval.workbench.__main__ import main

    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        if getattr(sys, "frozen", False) and "run" not in sys.argv:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                "启动失败，请查看 %LOCALAPPDATA%\\ExperimentWorkbench\\logs\\desktop.log",
                "Experiment Workbench",
                0x10,
            )
        raise


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    boot()
