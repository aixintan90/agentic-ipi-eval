"""Model metadata tests only; no inference or real experiment execution."""

import json
import subprocess
import threading
import urllib.request

import pytest

from cursor_dynamic_eval.workbench.adapters import CursorAdapter
from cursor_dynamic_eval.workbench.config import defaults, normalize
from cursor_dynamic_eval.workbench.models import (
    DEFAULT_CURSOR_MODEL,
    cursor_models,
    parse_cursor_models,
)
from cursor_dynamic_eval.workbench.server import create_server
from cursor_dynamic_eval.workbench.service import WorkbenchService

MODEL_OUTPUT = """Available models

auto - Auto (current, default)
cursor-grok-4.6-high-fast - Cursor Grok 4.6 Fast
cursor-grok-4.6-high - Cursor Grok 4.6

Tip: use --model <id>
"""


def test_exact_model_ids_and_ansi():
    result = parse_cursor_models("\x1b[32m" + MODEL_OUTPUT + "\x1b[0m")
    assert [row["id"] for row in result] == [
        "auto",
        "cursor-grok-4.6-high-fast",
        DEFAULT_CURSOR_MODEL,
    ]
    assert result[0]["label"] == "Auto"


@pytest.mark.parametrize(
    "output", ["", "Login required", "model - not a catalog", "Available models\n"]
)
def test_missing_catalog_is_not_auto(output):
    with pytest.raises(ValueError, match="模型列表"):
        parse_cursor_models(output)


def test_new_default_does_not_relabel_explicit_auto(tmp_path):
    assert defaults(tmp_path)["target"]["model"] == DEFAULT_CURSOR_MODEL
    assert normalize({"target": {"model": "auto"}}, tmp_path)["target"]["model"] == "auto"


def test_public_bundle_uses_synthetic_corpus_when_teacher_corpus_is_absent(tmp_path):
    assert defaults(tmp_path)["corpus"] == "config/corpora/workbench_example.json"


def test_internal_checkout_keeps_teacher_corpus_when_present(tmp_path):
    corpus = tmp_path / "config/corpora/teacher_new_windows_full.json"
    corpus.parent.mkdir(parents=True)
    corpus.write_text("{}", encoding="utf-8")
    assert defaults(tmp_path)["corpus"] == "config/corpora/teacher_new_windows_full.json"


def test_metadata_command_no_prompt_or_secret_forwarding(tmp_path, monkeypatch):
    config = defaults(tmp_path)
    config["target"]["bridge"] = "wsl"
    monkeypatch.setenv("SP27_MUTATION_API_KEY", "not-a-real-key")
    monkeypatch.setenv("XIAOAI_API_KEY", "another-fixture")
    monkeypatch.setenv("WSLENV", "SP27_MUTATION_API_KEY/u:KEEP/u")
    calls = []

    def metadata(argv, **kwargs):
        calls.append(argv)
        assert "--list-models" in argv[-1]
        assert "--model " not in argv[-1]
        assert "SP27_MUTATION_API_KEY" not in kwargs["env"]
        assert "XIAOAI_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["WSLENV"] == "KEEP/u"
        assert kwargs["timeout"] == 30 and kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0, MODEL_OUTPUT, "")

    monkeypatch.setattr(subprocess, "run", metadata)
    result = cursor_models(config)
    assert len(calls) == 1 and len(result["models"]) == 3
    assert result["checked_at"] and result["source"] == "cursor-agent --list-models"


@pytest.mark.parametrize("mode", ["timeout", "exit", "missing"])
def test_catalog_errors_do_not_expose_diagnostics(tmp_path, monkeypatch, mode):
    def fail(argv, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired("secret-value", 30)
        if mode == "missing":
            raise OSError("secret-value")
        return subprocess.CompletedProcess(argv, 1, "secret-value", "secret-value")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError) as error:
        cursor_models(defaults(tmp_path))
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize("missing", [True, False])
def test_preflight_never_substitutes_model(tmp_path, monkeypatch, missing):
    config = defaults(tmp_path)
    config["target"]["bridge"] = "native"
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        if "--list-models" in argv:
            output = "Available models\nauto - Auto\n" if missing else MODEL_OUTPUT
        else:
            output = "READY"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(subprocess, "run", execute)
    checks = CursorAdapter().probe(config, live=True)
    check = next(c for c in checks if c["name"] == "被测模型可用性")
    assert check["ok"] is not missing
    inference = [argv for argv in calls if "--model" in argv]
    assert len(inference) == (0 if missing else 1)
    if inference:
        assert inference[0][inference[0].index("--model") + 1] == DEFAULT_CURSOR_MODEL
    assert config["target"]["model"] == DEFAULT_CURSOR_MODEL


def test_model_endpoint_is_read_only(tmp_path, monkeypatch):
    app = WorkbenchService(tmp_path, tmp_path / "workbench")
    monkeypatch.setattr(
        CursorAdapter,
        "list_models",
        lambda self, config: {
            "models": parse_cursor_models(MODEL_OUTPUT),
            "source": "offline fixture",
            "checked_at": "now",
        },
    )
    server = create_server(tmp_path, port=0, service=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/models",
            data=json.dumps({"target": {"bridge": "native"}}).encode(),
            headers={"Content-Type": "application/json", "X-Workbench": "1"},
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        assert result["ok"] and len(result["models"]) == 3
        assert not list(app.root.iterdir())
    finally:
        server.shutdown()
        server.server_close()
