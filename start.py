#!/usr/bin/env python3
"""Cross-platform launcher for the unified video annotation workbench."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import quick_labeler
import reviewer
from project_app import ProjectApp, ProjectHandler
from launcher_server import run_launcher


SETTINGS_FILE = Path.home() / ".video_reviewer_launcher.json"
MAX_RECENT_PROJECTS = 10


def choose_port(host: str, preferred: int = 8765) -> int:
    """Choose the first free port so a stale/other service cannot block startup."""
    for port in range(preferred, preferred + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((host, port))
            return port
        except OSError:
            continue
    raise OSError(f"找不到可用端口（已检查 {preferred}–{preferred + 99}）")


def clean_path(value: str) -> Path:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return Path(value).expanduser()


def default_outputs(source: Path) -> dict[str, Path]:
    return {
        "output": source / "output",
        "no_fall_output": source / "no_fall_output",
        "fall_output": source / "fall_output",
        "caregiver_fall_output": source / "caregiver_fall_output",
    }


def load_settings() -> dict[str, Any]:
    empty: dict[str, Any] = {"recent_projects": [], "projects": {}, "last_mode": "project"}
    try:
        loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(loaded, dict):
        return empty
    recent = loaded.get("recent_projects", [])
    projects = loaded.get("projects", {})
    return {
        "recent_projects": [str(item) for item in recent if isinstance(item, str)][:MAX_RECENT_PROJECTS],
        "projects": projects if isinstance(projects, dict) else {},
        "last_mode": loaded.get("last_mode") if loaded.get("last_mode") in {"clip", "label", "project"} else "project",
    }


def save_settings(selection: dict[str, str]) -> None:
    settings = load_settings()
    source = selection["source"]
    recent = [source] + [item for item in settings["recent_projects"] if item != source]
    settings["recent_projects"] = recent[:MAX_RECENT_PROJECTS]
    settings["last_mode"] = selection["mode"]
    settings["projects"][source] = {key: value for key, value in selection.items() if key not in {"source", "projectConfig"}}
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(SETTINGS_FILE)
    except OSError as exc:
        print(f"提示：未能保存最近项目列表：{exc}", file=sys.stderr)


def choose_in_terminal() -> dict[str, str]:
    settings = load_settings()
    recent = settings["recent_projects"]
    if recent:
        print("\n最近使用的项目：")
        for index, item in enumerate(recent, 1):
            suffix = "" if Path(item).is_dir() else "（当前不可访问）"
            print(f"  {index}. {item}{suffix}")
    while True:
        prompt = "输入最近项目编号，或粘贴项目目录路径"
        if recent:
            prompt += "（直接回车使用第 1 个）"
        value = input(f"{prompt}：").strip()
        if not value and recent:
            value = "1"
        if value.isdigit() and 1 <= int(value) <= len(recent):
            value = recent[int(value) - 1]
        source = clean_path(value).resolve()
        if source.is_dir():
            break
        print(f"目录不存在：{source}")
    saved = settings["projects"].get(str(source), {})
    default_choice = {'fall': '1', 'clips': '2', 'custom': '3'}.get(saved.get('preset'), '1')
    while True:
        value = input(f"新项目模板：1=跌倒分类，2=区间精选，3=通用标注 [{default_choice}]（已有项目恢复保存规则）：").strip()
        value = value or default_choice
        if value in {"1", "2", "3"}:
            template = {'1': 'fall', '2': 'clips', '3': 'custom'}[value]
            break
        print("请输入 1、2 或 3。")
    defaults = default_outputs(source)

    def ask_path(label: str, default: Path) -> str:
        value = input(f"{label} [{default}]：").strip()
        return str(clean_path(value).resolve() if value else default.resolve())

    result = {'source': str(source), 'mode': 'project', 'preset': template, 'output_root': ask_path('新项目输出根目录，其他规则进入页面后可修改', defaults['output'])}
    save_settings(result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="选择一个项目目录并启动视频人工审核工具（只扫描目录第一层）")
    parser.add_argument("--source", help="包含待审核视频的项目目录；不提供时打开浏览器项目中心")
    parser.add_argument("--mode", choices=("clip", "label", "project"), help="兼容旧参数：clip=区间模板；label=跌倒模板；project=通用项目")
    parser.add_argument("--preset", choices=("fall", "clips", "custom"), help="新项目模板；已存在项目按保存规则恢复")
    parser.add_argument("--output-root", help="新项目输出根目录；默认项目目录/output")
    parser.add_argument("--output", help="8 秒片段输出目录；默认是项目目录/output")
    parser.add_argument("--no-fall-output", help="全程无跌倒输出目录；默认是项目目录/no_fall_output")
    parser.add_argument("--fall-output", help="整段 Fall 视频输出目录；默认是项目目录/fall_output")
    parser.add_argument("--caregiver-fall-output", help="护工 Fall 视频输出目录；默认是项目目录/caregiver_fall_output")
    parser.add_argument("--no-gui", action="store_true", help="使用终端输入路径，不显示项目选择窗口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--desktop", action="store_true", help="在独立应用窗口打开，需要桌面组件")
    parser.add_argument("--browser", action="store_true", help="使用默认浏览器，免安装版也不创建独立窗口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机；局域网共享可用 0.0.0.0")
    parser.add_argument("--port", type=int, help="网页端口；不提供时从 8765 开始自动选择空闲端口")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    lifecycle = parser.add_mutually_exclusive_group()
    lifecycle.add_argument("--close-on-tab-close", action="store_true", help="关闭最后一个审核页面后退出服务")
    lifecycle.add_argument("--keep-running", action="store_true", help="关闭页面后继续保留后台服务")
    return parser.parse_args()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = parse_args()
    if args.self_test:
        required = ["project_center.html", "workbench.html", "workbench.js", "workbench.css", "media.js", "app.js", "playback.js", "layout.css"]
        missing = [name for name in required if not Path(__file__).with_name(name).is_file()]
        if missing:
            print(f"自检失败：缺少资源文件：{', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            result = reviewer.run_command(["ffmpeg", "-version"], timeout=30)
        except (OSError, RuntimeError) as exc:
            print(f"自检失败：{exc}", file=sys.stderr)
            return 2
        if result.returncode != 0:
            print(f"自检失败：内置 FFmpeg 无法运行：{result.stderr}", file=sys.stderr)
            return 2
        # Exercise bundled asset routing and windowed HTTP logging too.
        from http.client import HTTPConnection
        from threading import Thread
        server = quick_labeler.LabelServer(("127.0.0.1", 0), None)
        server.RequestHandlerClass = ProjectHandler
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for route in ("/", "/assets/workbench.js", "/assets/workbench.css", "/assets/media.js", "/assets/app.js", "/assets/playback.js", "/api/runtime"):
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("GET", route)
                    response = connection.getresponse()
                    if response.status != 200 or not response.read():
                        raise RuntimeError(f"页面资源无法访问：{route}")
                finally:
                    connection.close()
        except Exception as exc:
            print(f"自检失败：{exc}", file=sys.stderr)
            return 2
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        print("自检通过：网页服务、页面资源和内置 FFmpeg 均可用。")
        return 0
    if args.port is None:
        args.port = choose_port(args.host)
        if args.port != 8765:
            print(f"端口 8765 正在使用，已自动改用 {args.port}。")
    if args.host not in {"127.0.0.1", "localhost"}:
        print("提醒：当前服务允许其他设备访问，请只在可信局域网中使用。")
    remote_session = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("VSCODE_IPC_HOOK_CLI"))
    # Source, packaged and remote launches share the same page lifetime.
    # --no-browser controls opening a browser, not keeping a service alive.
    auto_close = not args.keep_running
    from desktop_window import available as desktop_available
    desktop = not (args.no_browser or args.browser or remote_session) and (args.desktop or bool(getattr(sys, 'frozen', False))) and desktop_available()
    selection = None
    if args.source:
        source = clean_path(args.source).resolve()
        defaults = default_outputs(source)
        selection = {
            "source": str(source), "mode": args.mode or "clip",
            "output": str(clean_path(args.output).resolve() if args.output else defaults["output"].resolve()),
            "no_fall_output": str(clean_path(args.no_fall_output).resolve() if args.no_fall_output else defaults["no_fall_output"].resolve()),
            "fall_output": str(clean_path(args.fall_output).resolve() if args.fall_output else defaults["fall_output"].resolve()),
            "caregiver_fall_output": str(clean_path(args.caregiver_fall_output).resolve() if args.caregiver_fall_output else defaults["caregiver_fall_output"].resolve()),
        }
        selection["preset"] = args.preset or ("clips" if args.mode == "clip" else "fall")
        selection["output_root"] = args.output_root or ""
        for flag, key in ((args.output, "output"), (args.no_fall_output, "no_fall_output"), (args.fall_output, "fall_output"), (args.caregiver_fall_output, "caregiver_fall_output")):
            if not flag:
                selection.pop(key, None)
    elif args.no_gui:
        selection = choose_in_terminal()
    def prepare_project(project, progress):
        progress("检查视频组件", 0, None, "正在检查 FFmpeg")
        reviewer.ffmpeg_executable()
        if desktop and sys.platform.startswith('linux'):
            project = {**project, 'preview_format': 'webm'}
        source = Path(project["source"])
        print(f"本次项目：{source}（只扫描第一层）", flush=True)
        return ProjectApp(source, project, progress), ProjectHandler

    try:
        run_launcher(
            args.host, args.port, load_settings(), Path(__file__).with_name("project_center.html"),
            save_settings, open_browser=not args.no_browser and not remote_session and not desktop,
            prepare_project=prepare_project, initial_selection=selection, auto_close=auto_close, desktop=desktop,
        )
    finally:
        reviewer.stop_background_commands()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
