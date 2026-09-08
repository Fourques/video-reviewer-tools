#!/usr/bin/env python3
"""Small local web launcher used before either review application starts."""

from __future__ import annotations

import json
import os
import string
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from app_runtime import AppRuntime, RuntimeHandlerMixin, safe_log


def _directory_roots() -> list[str]:
    roots: list[Path] = [Path.home()]
    if os.name == "nt":
        roots.extend(Path(f"{letter}:\\") for letter in string.ascii_uppercase if Path(f"{letter}:\\").is_dir())
    else:
        roots.append(Path("/"))
        volumes = Path("/Volumes")
        if volumes.is_dir():
            roots.extend(item for item in volumes.iterdir() if item.is_dir())
        mounts = Path("/mnt")
        if mounts.is_dir():
            roots.extend(item for item in mounts.iterdir() if item.is_dir())
    unique: list[str] = []
    for item in roots:
        try:
            value = str(item.resolve())
        except OSError:
            continue
        if value not in unique:
            unique.append(value)
    return unique


class LauncherApp:
    def __init__(
        self,
        settings: dict[str, Any],
        html_path: Path,
        save_selection: Callable[[dict[str, str]], None],
    ) -> None:
        self.settings = settings
        self.html = html_path.read_bytes()
        self.save_selection = save_selection
        self.selection: dict[str, str] | None = None

    def config(self) -> dict[str, Any]:
        return {
            "recentProjects": self.settings.get("recent_projects", []),
            "projects": self.settings.get("projects", {}),
            "lastMode": self.settings.get("last_mode", "clip"),
            "home": str(Path.home()),
            "cwd": str(Path.cwd()),
            "roots": _directory_roots(),
            "platform": os.name,
        }

    def directories(self, raw_path: str) -> dict[str, Any]:
        path = Path(raw_path).expanduser() if raw_path.strip() else Path.home()
        path = path.resolve()
        if not path.is_dir():
            raise ValueError(f"目录不存在：{path}")
        try:
            children = sorted(
                (
                    item for item in path.iterdir()
                    if not item.name.startswith(".") and item.is_dir()
                ),
                key=lambda item: item.name.casefold(),
            )
        except PermissionError as exc:
            raise ValueError(f"没有权限读取目录：{path}") from exc
        parent = path.parent if path.parent != path else None
        return {
            "path": str(path),
            "parent": str(parent) if parent else None,
            "directories": [{"name": item.name, "path": str(item)} for item in children[:1000]],
            "truncated": len(children) > 1000,
        }

    def validate_selection(self, payload: dict[str, Any]) -> dict[str, str]:
        source_value = str(payload.get("source", "")).strip().strip("\"'")
        if not source_value:
            raise ValueError("请选择项目目录")
        source = Path(source_value).expanduser().resolve()
        if not source.is_dir():
            raise ValueError(f"项目目录不存在：{source}")
        mode = str(payload.get("mode", ""))
        if mode not in {"clip", "label"}:
            raise ValueError("请选择审核功能")

        def output_path(key: str, default_name: str) -> Path:
            raw = str(payload.get(key, "")).strip().strip("\"'")
            return (Path(raw).expanduser() if raw else source / default_name).resolve()

        output = output_path("output", "output")
        no_fall_output = output_path("no_fall_output", "no_fall_output")
        fall_output = output_path("fall_output", "fall_output")
        caregiver_fall_output = output_path("caregiver_fall_output", "caregiver_fall_output")
        active = [output, no_fall_output]
        if mode == "clip" and source in active:
            raise ValueError("输出目录不能与项目目录完全相同")
        if mode == "clip" and output == no_fall_output:
            raise ValueError("片段与无跌倒输出目录不能相同")
        if mode == "label" and len({fall_output, no_fall_output, caregiver_fall_output}) != 3:
            raise ValueError("跌倒、不跌倒与护工 Fall 输出目录必须互不相同")
        return {
            "source": str(source), "mode": mode,
            "output": str(output), "no_fall_output": str(no_fall_output),
            "fall_output": str(fall_output),
            "caregiver_fall_output": str(caregiver_fall_output),
        }


class LauncherServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: LauncherApp, prepare_project=None, auto_close=False) -> None:
        super().__init__(address, LauncherHandler)
        self.app = app
        self.launcher_app = app
        self.prepare_project = prepare_project
        self.runtime = AppRuntime(auto_close)
        self.start_lock = threading.Lock()

    def start_selection(self, selection):
        if not self.start_lock.acquire(blocking=False):
            raise ValueError("正在检索视频，请等待当前扫描完成")
        self.runtime.report("准备项目", 0, None, selection["source"])

        def prepare():
            try:
                app, handler = self.prepare_project(selection, self.runtime.report)
                if self.runtime.stopped.is_set():
                    close = getattr(app, "close", None)
                    if close:
                        close()
                    return
                self.app = app
                self.RequestHandlerClass = handler
                with self.runtime.lock:
                    self.runtime.progress = {"status": "ready", "stage": "检索完成", "done": len(app.videos), "total": len(app.videos), "message": "即将进入审核页面"}
                safe_log(f"已找到 {len(app.videos)} 个视频，审核页面已就绪。")
            except Exception as exc:
                with self.runtime.lock:
                    self.runtime.progress.update(status="error", stage="检索失败", message=str(exc))
                safe_log(f"项目启动失败：{exc}")
            finally:
                self.start_lock.release()
        threading.Thread(target=prepare, daemon=True, name="project-scan").start()


class LauncherHandler(RuntimeHandlerMixin, BaseHTTPRequestHandler):
    server: LauncherServer

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.runtime_get():
            return
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/launcher"}:
            body = self.server.launcher_app.html
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/config":
            self._json(self.server.launcher_app.config())
            return
        if parsed.path == "/api/directories":
            try:
                value = parse_qs(parsed.query).get("path", [""])[0]
                self._json(self.server.launcher_app.directories(value))
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self._json({"error": "Not Found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.runtime_post():
            return
        route = urlparse(self.path).path
        if route == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if route != "/api/start":
            self._json({"error": "Not Found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            selection = self.server.launcher_app.validate_selection(payload)
            self.server.launcher_app.save_selection(selection)
            self.server.launcher_app.selection = selection
            self.server.start_selection(selection)
            self._json({"ok": True, "message": "正在扫描视频，审核页面准备好后会自动进入。"})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format_string: str, *args: object) -> None:
        safe_log(f"[项目中心] {format_string % args}")


def run_launcher(
    host: str,
    port: int,
    settings: dict[str, Any],
    html_path: Path,
    save_selection: Callable[[dict[str, str]], None],
    open_browser: bool,
    prepare_project: Callable,
    initial_selection=None,
    auto_close=False,
) -> None:
    app = LauncherApp(settings, html_path, save_selection)
    server = LauncherServer((host, port), app, prepare_project, auto_close)
    shown_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{shown_host}:{port}"
    safe_log(f"项目选择页面：{url}")
    safe_log("请选择项目、审核功能和输出位置。按 Ctrl+C 可退出。")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url, new=2)).start()
    server.runtime.watch(server)
    if initial_selection:
        server.start_selection(initial_selection)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        return None
    finally:
        server.runtime.stopped.set()
        server.server_close()
        while getattr(server.app, "export_job", {}).get("status") == "running":
            time.sleep(0.2)
        close = getattr(server.app, "close", None)
        if close:
            close()
