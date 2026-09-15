from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from ..paths import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser(description="Local configurable experiment workbench")
    parser.add_argument("command", nargs="?", choices=["serve", "run"], default="serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--experiment", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.command == "run":
        if not args.experiment:
            parser.error("run requires --experiment")
        from .engine import run

        run(args.experiment.resolve())
    else:
        from .server import create_server
        from .service import WorkbenchService

        if getattr(sys, "frozen", False) and not args.data_root:
            args.data_root = (
                Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                / "ExperimentWorkbench"
                / "experiments"
            )
        for port in range(args.port, args.port + 20):
            try:
                server = create_server(
                    args.project,
                    host=args.host,
                    port=port,
                    service=WorkbenchService(args.project, args.data_root),
                )
                break
            except OSError:
                if port == args.port + 19:
                    raise
        print(f"Experiment workbench: http://{args.host}:{server.server_port}", flush=True)
        if getattr(sys, "frozen", False) and not args.no_browser:
            webbrowser.open(f"http://{args.host}:{server.server_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
