from __future__ import annotations

from cursor_dynamic_eval.workbench.desktop import DesktopApi, run_desktop


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeWindow:
    def __init__(self):
        self.events = type("Events", (), {"closing": FakeEvent()})()
        self.destroyed = False

    def destroy(self):
        self.destroyed = True

    def create_file_dialog(self, *args, **kwargs):
        return self.save_path


class FakeWebview:
    SAVE_DIALOG = 30

    def __init__(self):
        self.window = FakeWindow()
        self.created = None
        self.started = None

    def create_window(self, title, url, **options):
        self.created = (title, url, options)
        return self.window

    def start(self, **options):
        self.started = options


def test_desktop_shell_opens_local_service_without_browser_chrome(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    fake = FakeWebview()

    run_desktop(project, data_root=tmp_path / "data", port=0, webview_module=fake)

    title, url, options = fake.created
    assert title == "Agentic IPI Workbench"
    assert url.startswith("http://127.0.0.1:")
    assert options["min_size"] == (960, 640)
    assert options["zoomable"] is False
    assert fake.started["gui"] == "edgechromium"
    assert fake.started["debug"] is False


def test_desktop_api_copies_export_to_user_selected_path(tmp_path):
    source = tmp_path / "report.md"
    source.write_text("report", encoding="utf-8")
    destination = tmp_path / "chosen" / "my-report.md"

    class Service:
        def download(self, identifier, filename):
            assert identifier == "exp-1"
            assert filename == "report.md"
            return source

    window = FakeWindow()
    window.save_path = str(destination)
    api = DesktopApi(Service(), {"window": window}, FakeWebview())

    assert api.save_export("exp-1", "report.md") == {"saved": True, "path": str(destination)}
    assert destination.read_text(encoding="utf-8") == "report"
