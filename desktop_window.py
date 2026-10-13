"""Optional native app shell; remote/browser launch stays dependency-free.

Packaged desktop launches use an owned window, not a random browser tab. Closing
the window waits for pending label saves and explicitly shuts down the HTTP server.
"""
from __future__ import annotations

import threading
import sys
import os
import subprocess
import webbrowser
from pathlib import Path


def available():
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False


def require_modern_windows_renderer():
    """pywebview's forced Edge setting still silently falls back to MSHTML.

    Initialize before creating any window/HTTP worker, then check the *actual*
    renderer. WinForms setup_app is idempotent in the pinned pywebview version.
    """
    from webview.guilib import initialize
    backend = initialize('edgechromium')
    if getattr(backend, 'renderer', None) != 'edgechromium':
        raise RuntimeError('现代窗口内核不可用（WebView2 或 .NET 组件缺失/不可用），已禁止退回旧 IE 内核')
    return backend


def modern_windows_browsers():
    """Known browser executables, never shell commands or an IE association."""
    import winreg
    seen = set()
    for executable, relative in (
        ('msedge.exe', Path('Microsoft/Edge/Application/msedge.exe')),
        ('chrome.exe', Path('Google/Chrome/Application/chrome.exe')),
        ('firefox.exe', Path('Mozilla Firefox/firefox.exe')),
    ):
        candidates = []
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}', 0, winreg.KEY_READ | view) as key:
                        raw = winreg.QueryValueEx(key, None)[0]
                        if isinstance(raw, str):
                            candidates.append(Path(os.path.expandvars(raw.strip().strip('"'))))
                except OSError:
                    pass
        candidates.extend(Path(os.environ[name]) / relative for name in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA') if os.environ.get(name))
        for candidate in candidates:
            if candidate not in seen and candidate.is_file():
                seen.add(candidate)
                yield candidate


def open_review_browser(url):
    if sys.platform == 'win32':
        for executable in modern_windows_browsers():
            try:
                subprocess.Popen([str(executable), '--new-window', url], close_fds=True,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                return
            except OSError:
                continue
        raise RuntimeError('未找到可启动的 Edge、Chrome 或 Firefox。请安装 WebView2 Runtime 或现代浏览器后重试；原视频和审核进度未清空。')
    if not webbrowser.open(url, new=2):
        raise RuntimeError(f'未能打开浏览器，请手动访问：{url}')


def notify_browser_failure(message):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(message), 'VideoReviewer · 浏览器组件不可用', 0x10)


def serve_window(server, url, storage_path: Path):
    if sys.platform.startswith('linux'):
        os.environ.setdefault('QT_API', 'pyside6')
    import webview
    if sys.platform == 'win32':
        require_modern_windows_renderer()
    storage_path.mkdir(parents=True, exist_ok=True)
    window = webview.create_window("VideoReviewer · 视频标注工作台", url, width=1380, height=860,
                                   min_size=(960, 620), background_color="#11151c", text_select=True)
    allow_close = threading.Event()
    checking_close = threading.Lock()

    def closing():
        if allow_close.is_set():
            return True
        if not checking_close.acquire(blocking=False):
            return False
        # The closing event runs on the GUI thread. Synchronous JS evaluation
        # here deadlocks Qt/WebKit; reject once, check off-thread, then close.
        def check():
            try:
                if getattr(server.app, 'export_job', {}).get('status') == 'running':
                    window.evaluate_js("document.getElementById('organize')?.click()")
                    return
                try:
                    pending = window.evaluate_js("typeof state !== 'undefined' && Array.isArray(state.pending) ? state.pending.length : 0")
                    if pending:
                        window.evaluate_js("document.getElementById('saveState')?.click()")
                        return
                except Exception:
                    pass
                allow_close.set()
                window.destroy()
            finally:
                checking_close.release()
        threading.Thread(target=check, daemon=True).start()
        return False

    def closed():
        server.runtime.stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    window.events.closing += closing
    window.events.closed += closed
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    worker.start()

    def watch_service():
        worker.join()
        try:
            window.destroy()
        except Exception:
            pass
    threading.Thread(target=watch_service, daemon=True).start()
    try:
        gui = 'qt' if sys.platform.startswith('linux') else ('edgechromium' if sys.platform == 'win32' else None)
        webview.start(gui=gui, private_mode=False, storage_path=str(storage_path))
    finally:
        server.runtime.stopped.set()
        if worker.is_alive():
            server.shutdown()
        worker.join(5)
