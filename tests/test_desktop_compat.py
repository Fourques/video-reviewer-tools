"""No user windows: simulate the exact Windows silent IE fallback."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, build_opener, Request

import desktop_window
from app_runtime import AppRuntime
import start
from launcher_server import run_launcher


class DesktopCompatibilityTests(unittest.TestCase):
    def test_windows_defaults_to_browser_but_native_is_explicitly_available(self):
        for args, expected in (([], False), (['--desktop'], True), (['--desktop', '--browser'], False)):
            with self.subTest(args=args), patch.object(start.sys, 'platform', 'win32'), \
                 patch.object(start.sys, 'frozen', True, create=True), \
                 patch.object(start.sys, 'argv', ['start.py', '--port', '8879', *args]), \
                 patch.dict(start.os.environ, {}, clear=True), \
                 patch.object(desktop_window, 'available', return_value=True), \
                 patch.object(start, 'load_settings', return_value={}), \
                 patch.object(start.reviewer, 'stop_background_commands'), \
                 patch.object(start, 'run_launcher') as run:
                self.assertEqual(start.main(), 0)
                self.assertEqual(run.call_args.kwargs['desktop'], expected)
                self.assertEqual(run.call_args.kwargs['open_browser'], not expected)

    def test_browser_preference_respects_modern_default_and_ignores_ie(self):
        class Key:
            def __init__(self, path): self.path = path
            def __enter__(self): return self.path
            def __exit__(self, *args): pass
        for association, first in (('ChromeHTML', 'chrome.exe'), ('FirefoxURL', 'firefox.exe'), ('IE.HTTP', 'msedge.exe')):
            with self.subTest(association=association):
                def query(path, name):
                    return (association if name == 'ProgId' else '/browser/' + path.rsplit('\\', 1)[-1], 1)
                registry = SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2, KEY_READ=1, KEY_WOW64_64KEY=2, KEY_WOW64_32KEY=4,
                                           OpenKey=lambda hive, path, *args: Key(path), QueryValueEx=query)
                with patch.dict(sys.modules, {'winreg': registry}), patch.dict(desktop_window.os.environ, {}, clear=True), patch.object(Path, 'is_file', return_value=True):
                    browsers = list(desktop_window.modern_windows_browsers())
                self.assertEqual(browsers[0].name, first)
                self.assertEqual(len(browsers), 3)

    def test_blank_native_startup_recovers_but_never_closes_active_or_closed_page(self):
        runtime = AppRuntime()
        loaded, cancelled, recover = threading.Event(), threading.Event(), Mock()
        desktop_window.watch_native_startup(runtime, loaded, cancelled, recover, timeout=.001)
        recover.assert_called_once()
        for condition in ('loaded', 'cancelled', 'stopped', 'session'):
            with self.subTest(condition=condition):
                runtime = AppRuntime()
                loaded, cancelled, recover = threading.Event(), threading.Event(), Mock()
                if condition == 'loaded': loaded.set()
                if condition == 'cancelled': cancelled.set()
                if condition == 'stopped': runtime.stopped.set()
                if condition == 'session': runtime.enter('review')
                desktop_window.watch_native_startup(runtime, loaded, cancelled, recover, timeout=.001)
                recover.assert_not_called()

    def backend(self, renderer):
        return patch.dict(sys.modules, {'webview': SimpleNamespace(), 'webview.guilib': SimpleNamespace(initialize=Mock(return_value=SimpleNamespace(renderer=renderer)))})

    def test_actual_ie_renderer_rejected_even_when_edge_requested(self):
        for renderer in ('mshtml', 'cef', None):
            with self.subTest(renderer=renderer), self.backend(renderer):
                with self.assertRaisesRegex(RuntimeError, '旧 IE'):
                    desktop_window.require_modern_windows_renderer()

    def test_actual_edge_renderer_accepted(self):
        with self.backend('edgechromium'):
            self.assertEqual(desktop_window.require_modern_windows_renderer().renderer, 'edgechromium')

    def test_ie_rejected_before_window_creation_or_http_worker(self):
        fake = SimpleNamespace(create_window=Mock(), start=Mock())
        with patch.dict(sys.modules, {'webview': fake}), patch.object(desktop_window.sys, 'platform', 'win32'), \
             patch.object(desktop_window, 'require_modern_windows_renderer', side_effect=RuntimeError('IE blocked')):
            with self.assertRaisesRegex(RuntimeError, 'IE blocked'):
                desktop_window.serve_window(Mock(), 'http://127.0.0.1:1234', Path('unused'))
        fake.create_window.assert_not_called()
        fake.start.assert_not_called()

    def test_fallback_selects_modern_executable_without_shell(self):
        with patch.object(desktop_window.sys, 'platform', 'win32'), \
             patch.object(desktop_window, 'modern_windows_browsers', return_value=iter([Path('Edge/msedge.exe')])), \
             patch.object(desktop_window.subprocess, 'Popen') as popen, patch.object(desktop_window.webbrowser, 'open') as default:
            desktop_window.open_review_browser('http://127.0.0.1:1234')
        self.assertEqual(popen.call_args.args[0], [str(Path('Edge/msedge.exe')), '--new-window', 'http://127.0.0.1:1234'])
        self.assertNotIn('shell', popen.call_args.kwargs)
        default.assert_not_called()

    def test_failed_browser_tries_next_and_never_uses_ie_default(self):
        with patch.object(desktop_window.sys, 'platform', 'win32'), \
             patch.object(desktop_window, 'modern_windows_browsers', return_value=iter([Path('edge.exe'), Path('chrome.exe')])), \
             patch.object(desktop_window.subprocess, 'Popen', side_effect=[OSError('blocked'), Mock()]) as popen:
            desktop_window.open_review_browser('http://127.0.0.1:1234')
        self.assertEqual(popen.call_count, 2)
        with patch.object(desktop_window.sys, 'platform', 'win32'), \
             patch.object(desktop_window, 'modern_windows_browsers', return_value=iter([])), \
             patch.object(desktop_window.webbrowser, 'open') as default:
            with self.assertRaisesRegex(RuntimeError, 'WebView2'):
                desktop_window.open_review_browser('http://127.0.0.1:1234')
        default.assert_not_called()

    def test_failed_native_window_falls_back_once_and_releases_port(self):
        errors, addresses, workers = [], [], []
        def open_browser(url):
            addresses.append(url)
            def inspect():
                try:
                    opener = build_opener(ProxyHandler({}))
                    with opener.open(url + '/assets/browser_guard.js', timeout=5) as response:
                        self.assertIn(b'document.documentMode', response.read())
                    with opener.open(Request(url + '/api/quit', data=b'{}'), timeout=5) as response:
                        self.assertEqual(response.status, 200)
                except Exception as exc:
                    errors.append(str(exc))
            worker = threading.Thread(target=inspect, daemon=True)
            workers.append(worker)
            worker.start()
        with tempfile.TemporaryDirectory() as root, \
             patch.object(desktop_window, 'serve_window', side_effect=RuntimeError('IE blocked')), \
             patch.object(desktop_window, 'open_review_browser', side_effect=open_browser):
            page = Path(root) / 'page.html'
            page.write_text('test', encoding='utf-8')
            run_launcher('127.0.0.1', 0, {}, page, lambda _: None, False, Mock(), auto_close=True, desktop=True)
        for worker in workers:
            worker.join(5)
        self.assertEqual(len(addresses), 1)
        self.assertFalse(errors, errors)

    def test_browser_failure_reports_error_and_does_not_leave_server_running(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(desktop_window, 'serve_window', side_effect=RuntimeError('IE blocked')), \
             patch.object(desktop_window, 'open_review_browser', side_effect=RuntimeError('No modern browser')), \
             patch.object(desktop_window, 'notify_browser_failure') as notify:
            page = Path(root) / 'page.html'
            page.write_text('test', encoding='utf-8')
            self.assertIs(run_launcher('127.0.0.1', 0, {}, page, lambda _: None, False, Mock(), desktop=True), False)
            notify.assert_called_once()
