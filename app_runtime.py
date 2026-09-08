"""Shared startup reporting and local desktop browser lifetime."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ASSETS = Path(__file__).resolve().parent


def safe_log(message):
    """Diagnostic output must not break HTTP on non-Chinese Windows systems."""
    stream = sys.stdout
    if stream is None:
        return
    try:
        try:
            stream.write(message + "\n")
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "utf-8"
            stream.write((message + "\n").encode(encoding, errors="backslashreplace").decode(encoding))
        stream.flush()
    except OSError:
        pass


class AppRuntime:
    def __init__(self, auto_close=False, grace=8.0):
        self.auto_close = auto_close
        self.grace = grace
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.sessions = set()
        self.last_empty = time.monotonic()
        self.seen_page = False
        self.progress = {"status": "idle", "stage": "等待选择项目", "done": 0, "total": None, "message": ""}

    def report(self, stage, done=0, total=None, message=""):
        with self.lock:
            if self.stopped.is_set():
                raise RuntimeError("工具正在退出")
            if self.progress["status"] == "ready":
                return  # The initial progress page stays ready during later rescans.
            self.progress = {"status": "scanning", "stage": stage, "done": done, "total": total, "message": message}

    def snapshot(self):
        with self.lock:
            return dict(self.progress)

    def enter(self, page):
        with self.lock:
            self.sessions.add(page)
            self.seen_page = True

    def leave(self, page):
        with self.lock:
            if page in self.sessions:
                self.sessions.remove(page)
                if not self.sessions:
                    self.last_empty = time.monotonic()

    def should_close(self, busy=False, now=None):
        with self.lock:
            elapsed = (time.monotonic() if now is None else now) - self.last_empty
            return self.auto_close and not busy and not self.sessions and elapsed >= (self.grace if self.seen_page else 120)

    def watch(self, server):
        def monitor():
            while not self.stopped.wait(0.5):
                app = server.app
                # Let an original-video move/export finish before exiting.
                busy = getattr(app, "export_job", {}).get("status") == "running"
                if self.should_close(busy):
                    self.stopped.set()
                    server.shutdown()
                    return
        if self.auto_close:
            threading.Thread(target=monitor, daemon=True, name="browser-lifetime").start()


class RuntimeHandlerMixin:
    def runtime_get(self):
        route = urlparse(self.path)
        runtime = getattr(self.server, "runtime", None)
        if route.path in {"/assets/app.js", "/assets/layout.css"}:
            name = route.path.rsplit("/", 1)[-1]
            body = (ASSETS / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        if route.path == "/api/runtime":
            self.runtime_json({"autoClose": bool(runtime and runtime.auto_close)})
            return True
        if route.path == "/api/startup-status":
            self.runtime_json(runtime.snapshot() if runtime else {"status": "ready"})
            return True
        if route.path == "/api/session" and runtime and runtime.auto_close:
            page = parse_qs(route.query).get("id", [""])[0][:100]
            if not page:
                self.runtime_json({"error": "Missing page ID"}, 400)
                return True
            runtime.enter(page)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while not runtime.stopped.is_set():
                    self.wfile.write(b"data: connected\n\n")
                    self.wfile.flush()
                    runtime.stopped.wait(2)
            except (OSError, ConnectionError):
                pass
            finally:
                runtime.leave(page)
            return True
        return False

    def runtime_post(self):
        route = urlparse(self.path)
        runtime = getattr(self.server, "runtime", None)
        if route.path == "/api/session-close":
            # Drain beacon body so HTTP implementations can reuse connections.
            self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 1024))
            if runtime:
                runtime.leave(parse_qs(route.query).get("id", [""])[0][:100])
            self.runtime_json({"ok": True})
            return True
        return False

    def runtime_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
