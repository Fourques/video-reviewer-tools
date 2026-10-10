"""Export safety, long paths, non-blocking plans and durable resume regression."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from file_paths import extended_windows_path, io_path
from project_app import ProjectApp
from reviewer import copy_file_bytes


class ExportResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'project'
        self.source.mkdir()
        for name in ('one.mp4', 'two.mp4'):
            (self.source / name).write_bytes(name.encode() * 100)
        self.apps = []
        self.cache = patch('project_app.user_cache_dir', return_value=self.root / 'cache')
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def tearDown(self):
        for app in self.apps:
            app.close()
        # Windows TemporaryDirectory cleanup may itself lack long-path support.
        if os.name == 'nt':
            import shutil
            shutil.rmtree(io_path(self.root))
        self.temp.cleanup()

    def app(self):
        app = ProjectApp(self.source)
        self.apps.append(app)
        return app

    def mark(self, app, video=None):
        video = video or app.videos[0]
        app.save_annotation({'id': video.id, 'revision': app.annotation(video.id)['revision'], 'annotation': {**app.empty_annotation(), 'label': 'fall', 'status': 'done'}})

    def test_windows_unc_and_local_extended_paths(self):
        self.assertEqual(extended_windows_path(r'\\192.168.8.27\share\folder\video.mp4'), r'\\?\UNC\192.168.8.27\share\folder\video.mp4')
        self.assertEqual(extended_windows_path(r'C:\folder\video.mp4'), r'\\?\C:\folder\video.mp4')
        self.assertEqual(extended_windows_path(r'\\?\C:\folder\video.mp4'), r'\\?\C:\folder\video.mp4')

    def test_reported_temp_path_no_longer_contains_full_name(self):
        import ntpath
        name = 'kamicare_671322dc-244f-4032-a07f-a22b0ac78818_WFUSYA20LXB9UA250626_BATHROOM_DETECTED___1787588893_1787588903_25c8b22c-1805-4b2d-8bd8-969f94206a06.mp4'
        directory = r'\\192.168.8.27\1-B2B-data\2026\kamicare\bathroom\0928\bathroom\output\OTHER'
        self.assertEqual(len(ntpath.join(directory, '.' + name + '.' + 'a'*32 + '.tmp')), 263)
        temporary = ProjectApp._temporary_output(Path(name))
        self.assertLess(len(ntpath.join(directory, temporary.name)), 260)
        self.assertEqual(len(temporary.name), 40)
        self.assertTrue(ProjectApp._temporary_output(Path(name), clip=True).name.endswith('.tmp.mp4'))

    def test_long_basename_move_keeps_exact_name_and_bytes(self):
        name = 'kamicare_' + 'a'*220 + '.mp4'
        original = self.source / name
        io_path(original).write_bytes(b'exact original video bytes')
        app = self.app()
        video = next(video for video in app.videos if video.name == name)
        self.mark(app, video)
        app._export(app.export_plan()['items'])
        self.assertEqual(app.export_job['status'], 'done', app.export_job)
        self.assertEqual(io_path(self.source / 'output/fall' / name).read_bytes(), b'exact original video bytes')
        self.assertFalse(io_path(original).exists())
        self.assertFalse(list(io_path(self.source / 'output/fall').glob('.vr-*')))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows extended path filesystem regression')
    def test_real_windows_output_over_260_and_reopen(self):
        from reviewer import run_command
        original = self.source / 'one.mp4'
        original.unlink()
        result = run_command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=8', '-t', '1', '-c:v', 'libx264', str(original)])
        self.assertEqual(result.returncode, 0, result.stderr)
        content = original.read_bytes()
        app = self.app()
        video = app.videos[0]
        self.mark(app, video)
        output = self.root / ('a'*90) / ('b'*90) / ('c'*90)
        self.assertGreater(len(str(output)), 260)
        app.config['output'] = str(output)
        from project_store import atomic_json
        atomic_json(app.config_path, app.config)
        app._export(app.export_plan()['items'])
        self.assertEqual(app.export_job['status'], 'done', app.export_job)
        self.assertEqual(io_path(output / 'fall/one.mp4').read_bytes(), content)
        app.close()
        reopened = self.app()
        self.assertIn(video.id, reopened.video_by_id)
        self.assertEqual(reopened.annotation(video.id)['status'], 'done')
        self.assertGreater(reopened.video_info(video.id)['duration'], 0, 'FFmpeg cannot preview a long-path exported file')

    def test_plan_does_not_stat_resolve_or_hash_even_done_receipts(self):
        app = self.app()
        app.config['exportMode'] = 'copy'
        self.mark(app)
        app._export(app.export_plan()['items'])
        app.export_plan_cache = None
        with patch.object(Path, 'stat', side_effect=AssertionError('NAS stat in plan')), patch.object(Path, 'resolve', side_effect=AssertionError('NAS resolve in plan')), patch('project_app.sha256_file', side_effect=AssertionError('NAS hash in plan')):
            plan = app.export_plan()
            self.assertEqual(len(plan['checks']), 1)
            self.assertIs(plan, app.export_plan())

    def test_failed_move_restart_retries_only_remaining_keeps_labels(self):
        app = self.app()
        for video in app.videos:
            self.mark(app, video)
        identities = [video.id for video in app.videos]
        target = self.source / 'output/fall/one.mp4'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'external conflict')
        app._export(app.export_plan()['items'])
        history = app.export_history()['runs'][0]
        self.assertEqual((history['succeeded'], history['failureCount']), (1, 1))
        moved = self.source / 'output/fall/two.mp4'
        modified = moved.stat().st_mtime_ns
        app.close()
        reopened = self.app()
        self.assertTrue(all(reopened.annotation(identity)['status'] == 'done' for identity in identities))
        self.assertEqual(reopened.export_history()['runs'][0]['failureCount'], 1)
        target.unlink()  # only this synthetic external conflict belongs to test
        plan = reopened.export_plan()
        self.assertEqual(len(plan['items']), 1)
        reopened._export(plan['items'])
        self.assertEqual(reopened.export_job['status'], 'done')
        self.assertEqual(moved.stat().st_mtime_ns, modified, 'Already moved file was rewritten')
        self.assertEqual(len(reopened.export_history()['runs']), 2)
        self.assertFalse(reopened.export_plan()['items'])

    def test_interrupted_batch_is_visible_and_resumes(self):
        app = self.app()
        for video in app.videos:
            self.mark(app, video)
        def interrupted_copy(source, destination, progress=None):
            if source.name == 'two.mp4':
                raise SystemExit('simulate application crash')
            return copy_file_bytes(source, destination, progress)
        with patch('project_app.copy_file_bytes', side_effect=interrupted_copy), self.assertRaises(SystemExit):
            app._export(app.export_plan()['items'])
        app.close()
        reopened = self.app()
        history = reopened.export_history()['runs'][0]
        self.assertEqual(history['status'], 'interrupted')
        self.assertEqual(history['succeeded'], 1)
        self.assertEqual(len(reopened.export_plan()['items']), 1)
        reopened._export(reopened.export_plan()['items'])
        self.assertEqual(reopened.export_job['status'], 'done')

    def test_legacy_operations_are_shown_without_resetting_annotations(self):
        app = self.app()
        self.mark(app)
        app._export(app.export_plan()['items'])
        operations = {key: {field: value for field, value in record.items() if field != 'runId'} for key, record in app.state['operations'].items()}
        app.store.commit({'exportRuns': {}, 'operations': operations})
        app.close()
        reopened = self.app()
        self.assertEqual(reopened.export_history()['runs'][0]['status'], 'legacy')
        self.assertEqual(reopened.annotation(reopened.videos[0].id)['label'], 'fall')

    def test_same_file_symlink_output_never_deletes_source(self):
        app = self.app()
        self.mark(app)
        target = self.source / 'output/fall'
        target.parent.mkdir()
        try:
            target.symlink_to(self.source, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks not available for this user')
        app._export(app.export_plan()['items'])
        self.assertIn('同一文件', app.export_job['failures'][0]['error'])
        self.assertTrue((self.source / 'one.mp4').is_file())

    def test_missing_saved_file_is_failure_not_silent_all_done(self):
        app = self.app()
        self.mark(app)
        (self.source / 'one.mp4').unlink()
        app.scan()
        plan = app.export_plan()
        self.assertEqual(len(plan['items']), 1)
        app._export(plan['items'])
        self.assertEqual(app.export_job['failureCount'], 1)
        self.assertEqual(app.export_job['status'], 'error')

    def test_source_changed_during_copy_is_not_deleted(self):
        app = self.app()
        self.mark(app)
        source = self.source / 'one.mp4'
        def changing_copy(source, destination, progress=None):
            copy_file_bytes(source, destination, progress)
            source.write_bytes(b'changed while copying')
        with patch('project_app.copy_file_bytes', side_effect=changing_copy):
            app._export(app.export_plan()['items'])
        self.assertEqual(source.read_bytes(), b'changed while copying')
        self.assertEqual(app.export_job['status'], 'error')

    def test_progress_remains_reachable_while_disk_copy_waits(self):
        app = self.app()
        self.mark(app)
        entered, release = threading.Event(), threading.Event()
        def slow_copy(source, destination, progress=None):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test deadline')
            copy_file_bytes(source, destination, progress)
        with patch('project_app.copy_file_bytes', side_effect=slow_copy):
            app.start_export(app.state['seq'])
            try:
                self.assertTrue(entered.wait(3))
                began = time.monotonic()
                status = app.export_status()
                self.assertLess(time.monotonic()-began, .5)
                self.assertEqual(status['phase'], '复制临时文件')
                self.assertEqual(status['status'], 'running')
                with self.assertRaisesRegex(ValueError, '整理中'):
                    self.mark(app)
            finally:
                release.set()
            for _ in range(100):
                if app.export_status()['status'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(app.export_job['status'], 'done')

    def test_status_limits_errors_history_keeps_total(self):
        app = self.app()
        app.export_job['failures'] = [{'name': f'{index}.mp4', 'error': 'error'} for index in range(4092)]
        status = app.export_status()
        self.assertEqual(status['failureCount'], 4092)
        self.assertEqual(len(status['failures']), 50)
        self.assertTrue(status['failuresTruncated'])

    def test_batch_header_failure_does_not_leave_app_stuck_running(self):
        app = self.app()
        self.mark(app)
        with patch.object(app.store, 'commit', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                app.start_export(app.state['seq'])
        self.assertEqual(app.export_job['status'], 'error')
        self.assertTrue((self.source / 'one.mp4').exists())
        self.assertFalse(app.state['operations'])

    def test_missing_copy_output_can_be_recreated_from_saved_receipt(self):
        app = self.app()
        app.config['exportMode'] = 'copy'
        self.mark(app)
        app._export(app.export_plan()['items'])
        target = self.source / 'output/fall/one.mp4'
        target.unlink()
        plan = app.export_plan()
        self.assertEqual(len(plan['checks']), 1)
        app._export(plan['checks'])
        self.assertEqual(app.export_job['status'], 'done')
        self.assertEqual(target.read_bytes(), (self.source / 'one.mp4').read_bytes())

    def test_old_done_receipt_check_counts_verified_without_rewriting(self):
        app = self.app()
        app.config['exportMode'] = 'copy'
        self.mark(app)
        app._export(app.export_plan()['items'])
        target = self.source / 'output/fall/one.mp4'
        modified = target.stat().st_mtime_ns
        app._export(app.export_plan()['checks'])
        self.assertEqual(app.export_job['verified'], 1)
        self.assertEqual(app.export_job['succeeded'], 0)
        self.assertEqual(target.stat().st_mtime_ns, modified)
