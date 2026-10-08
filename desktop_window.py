"""Optional native app shell; remote/browser launch stays dependency-free.

Packaged desktop launches use an owned window, not a random browser tab. Closing
the window waits for pending label saves and explicitly shuts down the HTTP server.
"""
from __future__ import annotations

import threading
import sys
import os
from pathlib import Path


def available():
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False


def serve_window(server, url, storage_path: Path):
    if sys.platform.startswith('linux'):
        os.environ.setdefault('QT_API', 'pyside6')
    import webview
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
