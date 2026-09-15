from __future__ import annotations

import json
import mimetypes
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import VERSION
from .service import WorkbenchService

ASSETS = Path(__file__).parent / "static"


class LocalServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can let two live servers claim the same port.
    # The browser must never alternate between different experiment workspaces.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(project: Path, *, host="127.0.0.1", port=8765, service=None):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("实验控制台仅监听本机回环地址")
    app = service or WorkbenchService(project)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            expected = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            if self.headers.get("Host") not in expected:
                raise ValueError("仅接受本机访问")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                raise ValueError("拒绝跨站请求")

        def send_file(self, path, *, download=False):
            body = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                + ("; charset=utf-8" if path.suffix in {".js", ".css", ".html", ".md"} else ""),
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'",
            )
            if download:
                from urllib.parse import quote

                self.send_header(
                    "Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}"
                )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                self.allowed()
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                path = parsed.path
                if path == "/":
                    return self.send_file(ASSETS / "index.html")
                if path.startswith("/static/"):
                    file = (ASSETS / unquote(path[len("/static/") :])).resolve()
                    if ASSETS.resolve() not in file.parents or not file.is_file():
                        raise ValueError("资源不存在")
                    return self.send_file(file)
                if path == "/api/bootstrap":
                    value = app.bootstrap()
                elif path == "/api/health":
                    value = {
                        "app": "experiment-workbench",
                        "version": VERSION,
                        "data_root": str(app.root),
                    }
                elif path == "/api/experiments":
                    value = {"experiments": app.list()}
                elif path == "/api/experiment":
                    value = (
                        app.legacy_detail()
                        if query["id"] == "legacy-sp27"
                        else app.detail(query["id"])
                    )
                elif path == "/api/cases":
                    value = app.cases(query.pop("id"), **query)
                elif path == "/api/attempts":
                    value = app.attempts(query["id"], query["case_id"])
                elif path == "/api/download":
                    return self.send_file(app.download(query["id"], query["file"]), download=True)
                else:
                    return self.respond({"ok": False, "error": "页面不存在"}, 404)
                self.respond({"ok": True, **value})
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                self.respond({"ok": False, "error": str(exc)}, 400)

        def do_POST(self):
            try:
                self.allowed()
                if self.headers.get("X-Workbench") != "1" or not self.headers.get(
                    "Content-Type", ""
                ).startswith("application/json"):
                    raise ValueError("请求必须来自本地控制台")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32_000_000:
                    raise ValueError("无效请求大小")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("请求应是 JSON 对象")
                identifier = payload.get("id")
                path = urlparse(self.path).path
                if path == "/api/models":
                    value = app.models(payload["target"])
                elif path == "/api/save":
                    value = app.save(payload["config"], identifier)
                elif path == "/api/import-corpus":
                    value = app.import_corpus(payload["filename"], payload["content"])
                elif path == "/api/preview-corpus":
                    value = app.preview_corpus(payload["files"])
                elif path == "/api/confirm-corpus":
                    value = app.confirm_corpus(payload["token"])
                elif path == "/api/credential":
                    value = app.credential(identifier, payload["key"])
                elif path == "/api/egress-credential":
                    value = app.egress_credential(identifier, payload["channel"], payload["key"])
                elif path == "/api/test-delivery":
                    value = app.egress_test(identifier, payload["channel"])
                elif path == "/api/confirm-receipt":
                    value = app.confirm_receipt(
                        identifier, payload["run_id"], payload["received"], payload["message_id"]
                    )
                elif path == "/api/review":
                    value = app.review(identifier, payload["accepted"])
                elif path == "/api/preflight":
                    value = app.preflight(identifier, live=bool(payload.get("live")))
                elif path == "/api/start":
                    value = app.start(identifier)
                elif path == "/api/export":
                    value = app.export(identifier)
                elif path in {"/api/pause", "/api/stop"}:
                    value = app.control(identifier, path.rsplit("/", 1)[1])
                elif path == "/api/shutdown":
                    self.respond({"ok": True, "detail": "控制台服务已关闭，独立实验进程继续运行"})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                else:
                    return self.respond({"ok": False, "error": "未知操作"}, 404)
                self.respond({"ok": True, **value})
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                self.respond({"ok": False, "error": str(exc)}, 400)

    return LocalServer((host, port), Handler)
