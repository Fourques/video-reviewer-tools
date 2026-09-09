"""Launch policy regressions without touching projects or opening browsers."""
import os
import json
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from urllib.request import build_opener, ProxyHandler, Request
from unittest.mock import patch

import start


class LaunchLifetimeTests(unittest.TestCase):
    def test_remote_cli_releases_port_after_last_page_closes(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, str(Path(start.__file__)), '--no-browser', '--port', str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        opener = build_opener(ProxyHandler({}))
        base = f'http://127.0.0.1:{port}'
        streams = []
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with opener.open(base + '/api/runtime', timeout=1) as response:
                        self.assertTrue(json.load(response)['autoClose'])
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)
            for page in ('first', 'second'):
                stream = opener.open(base + '/api/session?id=' + page, timeout=5)
                streams.append(stream)
                self.assertIn(b'connected', stream.readline())
            with opener.open(Request(base + '/api/session-close?id=first', data=b'')) as response:
                response.read()
            time.sleep(.2)
            self.assertIsNone(process.poll())
            with opener.open(Request(base + '/api/session-close?id=second', data=b'')) as response:
                response.read()
            self.assertEqual(process.wait(timeout=15), 0)
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)
        finally:
            for stream in streams:
                stream.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    def test_close_on_last_page_is_default_for_all_launch_types(self):
        for frozen in (False, True):
            for remote in (False, True):
                for extra in ([], ['--no-browser'], ['--host', '0.0.0.0']):
                    with self.subTest(frozen=frozen, remote=remote, extra=extra):
                        env = {'SSH_CONNECTION': 'test'} if remote else {}
                        with patch.dict(os.environ, env, clear=True), \
                             patch.object(start.sys, 'frozen', frozen, create=True), \
                             patch.object(start.sys, 'argv', ['start.py', '--port', '8879', *extra]), \
                             patch.object(start, 'load_settings', return_value={}), \
                             patch.object(start, 'run_launcher') as run, \
                             patch.object(start.reviewer, 'stop_background_commands'):
                            self.assertEqual(start.main(), 0)
                            self.assertTrue(run.call_args.kwargs['auto_close'])
                            if remote or '--no-browser' in extra:
                                self.assertFalse(run.call_args.kwargs['open_browser'])

    def test_only_explicit_keep_running_leaves_service_running(self):
        with patch.object(start.sys, 'argv', ['start.py', '--port', '8879', '--keep-running']), \
             patch.object(start, 'load_settings', return_value={}), \
             patch.object(start, 'run_launcher') as run, \
             patch.object(start.reviewer, 'stop_background_commands'):
            start.main()
            self.assertFalse(run.call_args.kwargs['auto_close'])
