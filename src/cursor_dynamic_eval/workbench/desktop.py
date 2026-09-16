"""Native Windows shell for the local experiment workbench."""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from .server import create_server
from .service import WorkbenchService


class DesktopApi:
    """Native-only operations that require an explicit Windows file dialog."""

    def __init__(self, service: WorkbenchService, window_ref: dict[str, object], webview_module):
        self.service = service
        self.window_ref = window_ref
        self.webview = webview_module

    def save_export(self, identifier: str, filename: str) -> dict:
        source = self.service.download(identifier, filename).resolve()
        window = self.window_ref.get("window")
        if window is None:
            raise RuntimeError("桌面窗口尚未就绪")
        selected = window.create_file_dialog(
            self.webview.SAVE_DIALOG,
            directory=str(Path.home() / "Downloads"),
            save_filename=source.name,
        )
        if not selected:
            return {"saved": False, "path": ""}
        chosen = selected if isinstance(selected, str) else selected[0]
        destination = Path(chosen).resolve()
        if destination != source:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return {"saved": True, "path": str(destination)}


def default_data_root() -> Path:
    return (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "ExperimentWorkbench"
        / "experiments"
    )


def run_desktop(
    project: Path,
    *,
    data_root: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    webview_module=None,
) -> None:
    """Run the local server inside a native, browser-chrome-free window."""

    if webview_module is None:
        import webview as webview_module

    service = WorkbenchService(project, data_root or default_data_root())
    window_ref: dict[str, object] = {}
    desktop_api = DesktopApi(service, window_ref, webview_module)

    def close_window() -> None:
        window = window_ref.get("window")
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    server = None
    for candidate in range(port, port + 20):
        try:
            server = create_server(
                project,
                host=host,
                port=candidate,
                service=service,
                on_shutdown=close_window,
            )
            break
        except OSError:
            if candidate == port + 19:
                raise
    assert server is not None
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    window = webview_module.create_window(
        "Agentic IPI Workbench",
        f"http://{host}:{server.server_port}",
        width=1280,
        height=820,
        min_size=(960, 640),
        js_api=desktop_api,
        resizable=True,
        background_color="#F4F7F9",
        text_select=True,
        zoomable=False,
    )
    window_ref["window"] = window

    def stop_server() -> None:
        if server_thread.is_alive():
            threading.Thread(target=server.shutdown, daemon=True).start()

    window.events.closing += stop_server
    storage = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "ExperimentWorkbench"
        / "webview"
    )
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview_module.start(
            gui="edgechromium",
            debug=False,
            private_mode=True,
            storage_path=str(storage),
        )
    finally:
        server.shutdown()
        server.server_close()
