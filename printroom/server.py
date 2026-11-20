"""零依赖 HTTP 服务：JSON API + 单页网页。"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from . import db, service

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 1024 * 1024


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "PrintRoom/1.0"
    db_path: str = db.DEFAULT_DB

    # ------------------------------------------------------------------ 基础
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _error(self, exc: service.ServiceError) -> None:
        self._json({"error": exc.message, "code": exc.code}, exc.status)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise service.ServiceError("请求体过大", status=413, code="too_large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise service.ServiceError("请求体不是合法 JSON", status=400, code="bad_json")
        if not isinstance(data, dict):
            raise service.ServiceError("请求体必须是对象", status=400, code="bad_json")
        return data

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
                return
            if path == "/healthz":
                self._json({"ok": True})
                return
            if path == "/api/state":
                self._json(service.get_state(self.db_path))
                return
            m = re.fullmatch(r"/api/jobs/(\d+)", path)
            if m:
                self._json(service.get_job(self.db_path, int(m.group(1))))
                return
            m = re.fullmatch(r"/api/documents/(\d+)", path)
            if m:
                self._json(service.get_document(self.db_path, int(m.group(1))))
                return
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                self._serve_static(name)
                return
            self._json({"error": "接口不存在", "code": "not_found"}, 404)
        except service.ServiceError as exc:
            self._error(exc)
        except Exception as exc:  # pragma: no cover - 兜底
            self.log_message("unhandled error: %r", exc)
            self._json({"error": "服务器内部错误", "code": "internal"}, 500)

    def _serve_static(self, name: str) -> None:
        safe = os.path.normpath(name)
        if safe.startswith("..") or os.path.isabs(safe):
            self._json({"error": "文件不存在", "code": "not_found"}, 404)
            return
        full = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(full):
            self._json({"error": "文件不存在", "code": "not_found"}, 404)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        try:
            data = self._body()
            self._dispatch_post(path, data)
        except service.ServiceError as exc:
            self._error(exc)
        except Exception as exc:  # pragma: no cover - 兜底
            self.log_message("unhandled error: %r", exc)
            self._json({"error": "服务器内部错误", "code": "internal"}, 500)

    def _dispatch_post(self, path: str, data: dict[str, Any]) -> None:
        dbp = self.db_path

        def int_field(name: str) -> int:
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise service.ServiceError(f"缺少整数参数 {name}", 400, "invalid_field")
            try:
                return int(value)
            except ValueError:
                raise service.ServiceError(f"参数 {name} 必须是整数", 400, "invalid_field")

        if path == "/api/documents":
            result = service.create_document(dbp, str(data.get("title") or ""))
            self._json(result, 201)
            return

        m = re.fullmatch(r"/api/documents/(\d+)/pages", path)
        if m:
            result = service.add_page(dbp, int(m.group(1)), str(data.get("content") or ""))
            self._json(result, 201)
            return

        m = re.fullmatch(r"/api/documents/(\d+)/pages/last", path)
        if m:
            self._json(service.delete_last_page(dbp, int(m.group(1))))
            return

        m = re.fullmatch(r"/api/documents/(\d+)/finalize", path)
        if m:
            self._json(service.finalize_document(dbp, int(m.group(1))))
            return

        m = re.fullmatch(r"/api/jobs/(\d+)/start", path)
        if m:
            printer = str(data.get("printer_id") or "").strip()
            if not printer:
                raise service.ServiceError("请选择打印机", 400, "invalid_printer")
            self._json(service.start_job(dbp, int(m.group(1)), printer))
            return

        m = re.fullmatch(r"/api/jobs/(\d+)/print", path)
        if m:
            side = str(data.get("side") or "")
            expected = data.get("sheet_no")
            expected_no: Optional[int] = None
            if expected is not None:
                try:
                    expected_no = int(expected)
                except (TypeError, ValueError):
                    raise service.ServiceError("sheet_no 必须是整数", 400, "invalid_field")
            self._json(service.print_side(dbp, int(m.group(1)), side, expected_no))
            return

        m = re.fullmatch(r"/api/jobs/(\d+)/jam", path)
        if m:
            self._json(service.report_jam(dbp, int(m.group(1))))
            return

        m = re.fullmatch(r"/api/jobs/(\d+)/stop", path)
        if m:
            self._json(service.confirm_stopped(dbp, int(m.group(1))))
            return

        m = re.fullmatch(r"/api/jobs/(\d+)/switch", path)
        if m:
            printer = str(data.get("printer_id") or "").strip() or None
            self._json(service.switch_job(dbp, int(m.group(1)), printer))
            return

        self._json({"error": "接口不存在", "code": "not_found"}, 404)


def create_server(
    db_path: str = db.DEFAULT_DB,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> ThreadingHTTPServer:
    db.init_db(db_path)

    class BoundHandler(Handler):
        pass

    BoundHandler.db_path = db_path
    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    host = os.environ.get("PRINTROOM_HOST", "0.0.0.0")
    port = int(os.environ.get("PRINTROOM_PORT", "8000"))
    db_path = os.environ.get("PRINTROOM_DB", db.DEFAULT_DB)
    server = create_server(db_path, host, port)
    print(f"文印室打印队列已启动: http://{host}:{port}/  数据库: {db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
