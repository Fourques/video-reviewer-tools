#!/usr/bin/env python3
"""Cross-platform launcher for the two video review workflows."""

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
    empty: dict[str, Any] = {"recent_projects": [], "projects": {}, "last_mode": "clip"}
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
        "last_mode": loaded.get("last_mode") if loaded.get("last_mode") in {"clip", "label"} else "clip",
    }


def save_settings(selection: dict[str, str]) -> None:
    settings = load_settings()
    source = selection["source"]
    recent = [source] + [item for item in settings["recent_projects"] if item != source]
    settings["recent_projects"] = recent[:MAX_RECENT_PROJECTS]
    settings["last_mode"] = selection["mode"]
    settings["projects"][source] = {
        "mode": selection["mode"],
        "output": selection["output"],
        "no_fall_output": selection["no_fall_output"],
        "fall_output": selection["fall_output"],
        "caregiver_fall_output": selection["caregiver_fall_output"],
    }
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(SETTINGS_FILE)
    except OSError as exc:
        print(f"提示：未能保存最近项目列表：{exc}", file=sys.stderr)


def choose_with_gui() -> dict[str, str] | None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError("当前环境没有可用的图形桌面") from exc

    settings = load_settings()
    result: dict[str, str] | None = None
    root.title("视频人工审核工具")
    root.geometry("780x500")
    root.minsize(680, 470)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    style = ttk.Style(root)
    style.configure("Title.TLabel", font=("TkDefaultFont", 16, "bold"))
    style.configure("Section.TLabelframe.Label", font=("TkDefaultFont", 10, "bold"))
    style.configure("Start.TButton", font=("TkDefaultFont", 11, "bold"), padding=(18, 8))

    outer = ttk.Frame(root, padding=20)
    outer.grid(row=0, column=0, sticky="nsew")
    outer.columnconfigure(0, weight=1)

    ttk.Label(outer, text="选择一个视频审核项目", style="Title.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(
        outer,
        text="所有目录都只读取第一层。快速分类会联合读取项目目录和三个分类目录，方便复核旧分类。",
        foreground="#52606d",
    ).grid(row=1, column=0, sticky="w", pady=(5, 16))

    project_frame = ttk.LabelFrame(outer, text="1. 项目目录", style="Section.TLabelframe", padding=12)
    project_frame.grid(row=2, column=0, sticky="ew")
    project_frame.columnconfigure(0, weight=1)
    source_var = tk.StringVar()
    project_combo = ttk.Combobox(project_frame, textvariable=source_var, values=settings["recent_projects"])
    project_combo.grid(row=0, column=0, sticky="ew", padx=(0, 8))

    mode_var = tk.StringVar(value=settings["last_mode"])
    output_var = tk.StringVar()
    no_fall_var = tk.StringVar()
    fall_var = tk.StringVar()
    caregiver_fall_var = tk.StringVar()
    loaded_source = {"value": ""}

    def project_values(source_text: str, use_saved: bool = True) -> None:
        if not source_text.strip():
            output_var.set("")
            no_fall_var.set("")
            fall_var.set("")
            caregiver_fall_var.set("")
            return
        source = clean_path(source_text).resolve()
        loaded_source["value"] = str(source)
        defaults = default_outputs(source)
        saved = settings["projects"].get(str(source), {}) if use_saved else {}
        if use_saved and saved.get("mode") in {"clip", "label"}:
            mode_var.set(saved["mode"])
        output_var.set(str(saved.get("output") or defaults["output"]))
        no_fall_var.set(str(saved.get("no_fall_output") or defaults["no_fall_output"]))
        fall_var.set(str(saved.get("fall_output") or defaults["fall_output"]))
        caregiver_fall_var.set(str(saved.get("caregiver_fall_output") or defaults["caregiver_fall_output"]))

    def browse_project() -> None:
        initial = source_var.get().strip()
        initial_dir = initial if initial and clean_path(initial).is_dir() else None
        selected = filedialog.askdirectory(
            title="选择包含待审核视频的项目目录", mustexist=True, initialdir=initial_dir, parent=root
        )
        if selected:
            source_var.set(str(Path(selected).resolve()))
            project_values(selected)

    ttk.Button(project_frame, text="浏览…", command=browse_project).grid(row=0, column=1)
    ttk.Label(project_frame, text="可直接粘贴路径；下拉框中会保留最近 10 个项目。", foreground="#66788a").grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(7, 0)
    )
    def apply_project_if_changed() -> None:
        value = source_var.get().strip()
        if value and str(clean_path(value).resolve()) != loaded_source["value"]:
            project_values(value)

    project_combo.bind("<<ComboboxSelected>>", lambda _event: project_values(source_var.get()))
    project_combo.bind("<FocusOut>", lambda _event: apply_project_if_changed())

    mode_frame = ttk.LabelFrame(outer, text="2. 审核功能", style="Section.TLabelframe", padding=12)
    mode_frame.grid(row=3, column=0, sticky="ew", pady=(14, 0))
    mode_frame.columnconfigure((0, 1), weight=1)
    ttk.Radiobutton(
        mode_frame, text="固定 8 秒片段审核\n选择片段，或判定整段无跌倒",
        variable=mode_var, value="clip",
    ).grid(row=0, column=0, sticky="w", padx=(0, 18))
    ttk.Radiobutton(
        mode_frame, text="整段 Fall 快速分类 / 复核\n联合浏览四个目录，可修改旧分类并重新整理",
        variable=mode_var, value="label",
    ).grid(row=0, column=1, sticky="w")

    output_frame = ttk.LabelFrame(outer, text="3. 输出位置", style="Section.TLabelframe", padding=12)
    output_frame.grid(row=4, column=0, sticky="ew", pady=(14, 0))
    output_frame.columnconfigure(0, weight=1)

    def browse_output(variable: tk.StringVar, title: str) -> None:
        current = variable.get().strip()
        candidate = clean_path(current) if current else clean_path(source_var.get() or ".")
        initial = candidate if candidate.is_dir() else candidate.parent
        selected = filedialog.askdirectory(title=title, initialdir=str(initial), parent=root)
        if selected:
            variable.set(str(Path(selected).resolve()))

    clip_outputs = ttk.Frame(output_frame)
    clip_outputs.grid(row=0, column=0, sticky="ew")
    clip_outputs.columnconfigure(1, weight=1)
    ttk.Label(clip_outputs, text="8 秒片段：").grid(row=0, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(clip_outputs, textvariable=output_var).grid(row=0, column=1, sticky="ew")
    ttk.Button(clip_outputs, text="修改…", command=lambda: browse_output(output_var, "选择 8 秒片段输出目录")).grid(
        row=0, column=2, padx=(8, 0)
    )
    ttk.Label(clip_outputs, text="无跌倒原视频：").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(8, 0))
    ttk.Entry(clip_outputs, textvariable=no_fall_var).grid(row=1, column=1, sticky="ew", pady=(8, 0))
    ttk.Button(
        clip_outputs, text="修改…", command=lambda: browse_output(no_fall_var, "选择无跌倒原视频输出目录")
    ).grid(row=1, column=2, padx=(8, 0), pady=(8, 0))

    label_outputs = ttk.Frame(output_frame)
    label_outputs.columnconfigure(1, weight=1)
    ttk.Label(label_outputs, text="Fall 原视频：").grid(row=0, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(label_outputs, textvariable=fall_var).grid(row=0, column=1, sticky="ew")
    ttk.Button(label_outputs, text="修改…", command=lambda: browse_output(fall_var, "选择 Fall 输出目录")).grid(
        row=0, column=2, padx=(8, 0)
    )
    ttk.Label(label_outputs, text="不跌倒原视频：").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(8, 0))
    ttk.Entry(label_outputs, textvariable=no_fall_var).grid(row=1, column=1, sticky="ew", pady=(8, 0))
    ttk.Button(label_outputs, text="修改…", command=lambda: browse_output(no_fall_var, "选择不跌倒输出目录")).grid(
        row=1, column=2, padx=(8, 0), pady=(8, 0)
    )
    ttk.Label(label_outputs, text="护工 Fall 原视频：").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(8, 0))
    ttk.Entry(label_outputs, textvariable=caregiver_fall_var).grid(row=2, column=1, sticky="ew", pady=(8, 0))
    ttk.Button(label_outputs, text="修改…", command=lambda: browse_output(caregiver_fall_var, "选择护工 Fall 输出目录")).grid(
        row=2, column=2, padx=(8, 0), pady=(8, 0)
    )

    def show_mode(*_args: object) -> None:
        if mode_var.get() == "label":
            clip_outputs.grid_remove()
            label_outputs.grid(row=0, column=0, sticky="ew")
        else:
            label_outputs.grid_remove()
            clip_outputs.grid()

    mode_var.trace_add("write", show_mode)
    show_mode()

    actions = ttk.Frame(outer)
    actions.grid(row=5, column=0, sticky="ew", pady=(16, 0))
    actions.columnconfigure(1, weight=1)

    def reset_defaults() -> None:
        project_values(source_var.get(), use_saved=False)

    ttk.Button(actions, text="恢复默认输出", command=reset_defaults).grid(row=0, column=0, sticky="w")
    ttk.Button(actions, text="取消", command=root.destroy).grid(row=0, column=2, padx=(0, 10))

    def submit() -> None:
        nonlocal result
        source_text = source_var.get().strip()
        if not source_text:
            messagebox.showerror("缺少项目目录", "请先选择或粘贴项目目录。", parent=root)
            return
        source = clean_path(source_text).resolve()
        if not source.is_dir():
            messagebox.showerror("项目目录不存在", f"找不到目录：\n{source}", parent=root)
            return
        if loaded_source["value"] != str(source):
            project_values(str(source))
        if not output_var.get().strip() or not no_fall_var.get().strip() or not fall_var.get().strip() or not caregiver_fall_var.get().strip():
            project_values(str(source), use_saved=False)
        selection = {
            "source": str(source), "mode": mode_var.get(),
            "output": str(clean_path(output_var.get()).resolve()),
            "no_fall_output": str(clean_path(no_fall_var.get()).resolve()),
            "fall_output": str(clean_path(fall_var.get()).resolve()),
            "caregiver_fall_output": str(clean_path(caregiver_fall_var.get()).resolve()),
        }
        active_outputs = [selection["output"], selection["no_fall_output"]]
        if selection["mode"] == "clip" and selection["source"] in active_outputs:
            messagebox.showerror("输出位置错误", "输出目录不能与项目目录完全相同。", parent=root)
            return
        if selection["mode"] == "clip" and selection["output"] == selection["no_fall_output"]:
            messagebox.showerror("输出位置错误", "片段和无跌倒输出目录不能相同。", parent=root)
            return
        if selection["mode"] == "label" and len({selection["fall_output"], selection["no_fall_output"], selection["caregiver_fall_output"]}) != 3:
            messagebox.showerror("输出位置错误", "跌倒、不跌倒和护工 Fall 输出目录必须互不相同。", parent=root)
            return
        result = selection
        save_settings(selection)
        root.destroy()

    ttk.Button(actions, text="开始审核", style="Start.TButton", command=submit).grid(row=0, column=3)
    root.bind("<Return>", lambda _event: submit())

    if settings["recent_projects"]:
        source_var.set(settings["recent_projects"][0])
        project_values(settings["recent_projects"][0])
    root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2)
    y = max(0, (root.winfo_screenheight() - root.winfo_height()) // 2)
    root.geometry(f"+{x}+{y}")
    root.mainloop()
    return result


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
    saved_mode = saved.get("mode") if saved.get("mode") in {"clip", "label"} else settings["last_mode"]
    default_choice = "1" if saved_mode == "clip" else "2"
    while True:
        value = input(f"选择功能：1=固定 8 秒片段审核，2=整段 Fall 快速分类 [{default_choice}]：").strip()
        value = value or default_choice
        if value in {"1", "2"}:
            mode = "clip" if value == "1" else "label"
            break
        print("请输入 1 或 2。")
    defaults = default_outputs(source)

    def ask_path(label: str, default: Path) -> str:
        value = input(f"{label} [{default}]：").strip()
        return str(clean_path(value).resolve() if value else default.resolve())

    result = {
        "source": str(source), "mode": mode,
        "output": str(clean_path(saved.get("output") or str(defaults["output"])).resolve()),
        "no_fall_output": str(clean_path(saved.get("no_fall_output") or str(defaults["no_fall_output"])).resolve()),
        "fall_output": str(clean_path(saved.get("fall_output") or str(defaults["fall_output"])).resolve()),
        "caregiver_fall_output": str(clean_path(saved.get("caregiver_fall_output") or str(defaults["caregiver_fall_output"])).resolve()),
    }
    if mode == "label":
        result["fall_output"] = ask_path("Fall 原视频输出目录，直接回车使用推荐值", Path(result["fall_output"]))
        result["no_fall_output"] = ask_path("不跌倒原视频输出目录，直接回车使用推荐值", Path(result["no_fall_output"]))
        result["caregiver_fall_output"] = ask_path("护工 Fall 原视频输出目录，直接回车使用推荐值", Path(result["caregiver_fall_output"]))
    else:
        result["output"] = ask_path("8 秒片段输出目录，直接回车使用推荐值", Path(result["output"]))
        result["no_fall_output"] = ask_path("无跌倒原视频输出目录，直接回车使用推荐值", Path(result["no_fall_output"]))
    save_settings(result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="选择一个项目目录并启动视频人工审核工具（只扫描目录第一层）")
    parser.add_argument("--source", help="包含待审核视频的项目目录；不提供时打开浏览器项目中心")
    parser.add_argument("--mode", choices=("clip", "label"), help="clip=8 秒审核；label=整段 Fall 分类")
    parser.add_argument("--output", help="8 秒片段输出目录；默认是项目目录/output")
    parser.add_argument("--no-fall-output", help="全程无跌倒输出目录；默认是项目目录/no_fall_output")
    parser.add_argument("--fall-output", help="整段 Fall 视频输出目录；默认是项目目录/fall_output")
    parser.add_argument("--caregiver-fall-output", help="护工 Fall 视频输出目录；默认是项目目录/caregiver_fall_output")
    parser.add_argument("--no-gui", action="store_true", help="使用终端输入路径，不显示项目选择窗口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机；局域网共享可用 0.0.0.0")
    parser.add_argument("--port", type=int, help="网页端口；不提供时从 8765 开始自动选择空闲端口")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        required = ["launcher.html", "index.html", "quick_label.html"]
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
        print("自检通过：页面资源和内置 FFmpeg 均可用。")
        return 0
    if args.port is None:
        args.port = choose_port(args.host)
        if args.port != 8765:
            print(f"端口 8765 正在使用，已自动改用 {args.port}。")
    if args.host not in {"127.0.0.1", "localhost"}:
        print("提醒：当前服务允许其他设备访问，请只在可信局域网中使用。")
    remote_session = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("VSCODE_IPC_HOOK_CLI"))
    browser_already_open = False
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
    elif args.no_gui:
        selection = choose_in_terminal()
    else:
        try:
            selection = run_launcher(
                args.host,
                args.port,
                load_settings(),
                Path(__file__).with_name("launcher.html"),
                save_settings,
                open_browser=not args.no_browser and not remote_session,
            )
            browser_already_open = selection is not None
        except OSError as exc:
            print(f"无法启动项目选择页面（{exc}），改用终端输入。")
            selection = choose_in_terminal()
        if selection is None:
            print("已取消启动。")
            return 0

    source = Path(selection["source"])
    mode = selection["mode"]
    if not source.is_dir():
        print(f"错误：项目目录不存在：{source}", file=sys.stderr)
        return 2
    print("\n本次任务目录（仅扫描项目目录第一层）：")
    print(f"  项目目录：{source}")
    if mode == "label":
        fall_output = Path(selection["fall_output"])
        no_fall_output = Path(selection["no_fall_output"])
        caregiver_fall_output = Path(selection["caregiver_fall_output"])
        print("  功能：整段 Fall 快速分类")
        print(f"  Fall 输出：{fall_output}\n")
        print(f"  不跌倒输出：{no_fall_output}\n")
        print(f"  护工 Fall 输出：{caregiver_fall_output}\n")
        sys.argv = [str(Path(quick_labeler.__file__).resolve()), "--source", str(source), "--output", str(fall_output), "--no-fall-output", str(no_fall_output), "--caregiver-output", str(caregiver_fall_output), "--host", args.host, "--port", str(args.port)]
        if remote_session or args.no_browser or browser_already_open:
            sys.argv.append("--no-browser")
        return quick_labeler.main()

    output = Path(selection["output"])
    no_fall_output = Path(selection["no_fall_output"])
    print("  功能：固定 8 秒片段审核")
    print(f"  8 秒片段：{output}")
    print(f"  全程无跌倒：{no_fall_output}\n")
    sys.argv = [str(Path(reviewer.__file__).resolve()), "--source", str(source), "--output", str(output), "--no-fall-output", str(no_fall_output), "--host", args.host, "--port", str(args.port)]
    if remote_session or args.no_browser or browser_already_open:
        sys.argv.append("--no-browser")
    return reviewer.main()


if __name__ == "__main__":
    raise SystemExit(main())
