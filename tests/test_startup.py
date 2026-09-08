from __future__ import annotations

import json
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import Request, ProxyHandler, build_opener

from app_runtime import AppRuntime, safe_log
from launcher_server import LauncherApp, LauncherServer
from quick_labeler import LabelHandler
from scan_support import cached_durations


class LifetimeTests(unittest.TestCase):
    def test_logging_on_non_chinese_windows_codepage(self):
        data = io.BytesIO()
        stream = io.TextIOWrapper(data, encoding="cp1252")
        with patch("app_runtime.sys.stdout", stream):
            safe_log("视频目录检索完成")
        self.assertIn(b"\\u", data.getvalue())
        stream.detach()

    def test_last_tab_close_has_grace_and_refresh_cancels_exit(self):
        runtime = AppRuntime(True, grace=8)
        runtime.enter("first")
        runtime.enter("second")
        runtime.leave("first")
        self.assertFalse(runtime.should_close(now=runtime.last_empty + 100))
        runtime.leave("second")
        self.assertFalse(runtime.should_close(now=runtime.last_empty + 7))
        self.assertTrue(runtime.should_close(now=runtime.last_empty + 9))
        self.assertFalse(runtime.should_close(busy=True, now=runtime.last_empty + 9))
        runtime.enter("refreshed")
        self.assertFalse(runtime.should_close(now=runtime.last_empty + 100))

    def test_remote_service_does_not_exit_when_tabs_close(self):
        runtime = AppRuntime(False)
        runtime.enter("remote")
        runtime.leave("remote")
        self.assertFalse(runtime.should_close(now=runtime.last_empty + 10000))


class DurationCacheTests(unittest.TestCase):
    def test_reopen_reuses_cache_and_changed_file_is_reprobed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            video.write_bytes(b"first")
            probe = Mock(return_value=12.5)
            progress = Mock()
            cache = root / "durations.json"
            self.assertEqual(cached_durations([video], cache, probe, progress), [12.5])
            self.assertEqual(cached_durations([video], cache, probe, progress), [12.5])
            self.assertEqual(probe.call_count, 1)
            video.write_bytes(b"changed-video")
            cached_durations([video], cache, probe, progress)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(progress.call_args.args[1:3], (1, 1))


class StartupHttpTests(unittest.TestCase):
    def test_scan_status_stays_reachable_and_error_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            html = source / "launcher.html"
            html.write_text("<h1>Project scan</h1>")
            release_scan = threading.Event()
            fail = [True]

            def prepare(selection, report):
                report("读取视频时长", 1, 2, "example.mp4")
                release_scan.wait(5)
                if fail[0]:
                    raise ValueError("Test scan failure")
                return SimpleNamespace(videos=[]), LabelHandler

            server = LauncherServer(("127.0.0.1", 0), LauncherApp({}, html, lambda _: None), prepare)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            opener = build_opener(ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_port}"

            def get_status():
                with opener.open(base + "/api/startup-status", timeout=2) as response:
                    return json.load(response)

            def wait_status(expected):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    value = get_status()
                    if value["status"] == expected:
                        return value
                    time.sleep(0.01)
                self.fail(f"Never reached {expected}")

            try:
                payload = json.dumps({"source": str(source), "mode": "label"}).encode()
                with opener.open(Request(base + "/api/start", data=payload, headers={"Content-Type": "application/json"})) as response:
                    self.assertEqual(response.status, 200)
                status = wait_status("scanning")
                self.assertEqual((status["done"], status["total"]), (1, 2))
                with opener.open(base) as response:
                    self.assertIn(b"Project scan", response.read())
                release_scan.set()
                self.assertIn("failure", wait_status("error")["message"])
                fail[0] = False
                with opener.open(Request(base + "/api/start", data=payload)) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(wait_status("ready")["total"], 0)
                # Windowed Windows apps have no stderr; HTTP must still work.
                with patch("quick_labeler.sys.stderr", None):
                    with opener.open(base) as response:
                        self.assertIn("Fall".encode(), response.read())
            finally:
                release_scan.set()
                server.runtime.stopped.set()
                server.shutdown()
                server.server_close()
                worker.join(3)
