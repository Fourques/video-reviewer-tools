from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

from reviewer import run_command


class BackgroundCommandTests(unittest.TestCase):
    def test_windows_ffmpeg_disables_console_and_keeps_capture(self) -> None:
        with patch("reviewer.sys.platform", "win32"), \
                patch("reviewer.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch("reviewer.ffmpeg_executable", return_value="C:/App/ffmpeg.exe"), \
                patch("reviewer.subprocess.run") as launch:
            run_command(["ffmpeg", "-version"], timeout=10)
        self.assertEqual(launch.call_args.args[0], ["C:/App/ffmpeg.exe", "-version"])
        options = launch.call_args.kwargs
        self.assertEqual(options["creationflags"], 0x08000000)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.PIPE)
        self.assertEqual(options["timeout"], 10)

    def test_non_windows_does_not_receive_windows_options(self) -> None:
        with patch("reviewer.sys.platform", "linux"), patch("reviewer.subprocess.run") as launch:
            run_command(["example"])
        self.assertNotIn("creationflags", launch.call_args.kwargs)

    def test_background_command_keeps_output_and_failure_status(self) -> None:
        result = run_command([
            sys.executable, "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
        ], timeout=15)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), "out")
        self.assertEqual(result.stderr.strip(), "err")

    @unittest.skipUnless(sys.platform == "win32", "Requires real Windows console APIs")
    def test_real_windows_child_has_no_console(self) -> None:
        result = run_command([
            sys.executable, "-c",
            "import ctypes; print(bool(ctypes.windll.kernel32.GetConsoleWindow()))",
        ], timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")
