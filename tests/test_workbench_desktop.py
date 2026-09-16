from __future__ import annotations

from cursor_dynamic_eval.workbench.desktop import run_desktop


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


class FakeWebview:
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
