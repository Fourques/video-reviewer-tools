"""Large-directory regressions without creating 12000 real network files."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_runtime import AppRuntime
from project_app import ProjectApp


class LargeStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'project'
        self.source.mkdir()
        self.apps = []
        self.cache = patch('project_app.user_cache_dir', return_value=self.root / 'cache')
        self.cache.start()

    def tearDown(self):
        for app in self.apps:
            app.close()
        self.cache.stop()
        self.temp.cleanup()

    def app(self, **selection):
        app = ProjectApp(self.source, selection)
        self.apps.append(app)
        return app

    def test_12000_entries_no_per_video_resolve_or_second_stat(self):
        calls = {'is_file': 0, 'stat': 0}
        class Entry:
            def __init__(self, index):
                self.name = f'video_{index:05d}.mp4'
            def is_file(self, **kwargs):
                calls['is_file'] += 1
                return True
            def stat(self):
                calls['stat'] += 1
                return SimpleNamespace(st_size=123, st_mtime_ns=1000000)
        class Entries:
            def __enter__(self):
                return (Entry(index) for index in range(12000))
            def __exit__(self, *args):
                pass
        original_scan, original_resolve, original_exists = os.scandir, Path.resolve, Path.exists
        def scan(directory):
            return Entries() if Path(directory) == self.source else original_scan(directory)
        def resolve(path, *args, **kwargs):
            self.assertNotEqual(path.suffix, '.mp4', 'Network realpath performed per video')
            return original_resolve(path, *args, **kwargs)
        def exists(path):
            self.assertNotEqual(path.suffix, '.mp4', 'Repeated existence probe of scanned video')
            return original_exists(path)
        progress = []
        with patch('project_app.os.scandir', side_effect=scan), patch.object(Path, 'resolve', resolve), patch.object(Path, 'exists', exists):
            app = ProjectApp(self.source, progress=lambda *values: progress.append(values))
            self.apps.append(app)
            self.assertEqual(len(app.videos), 12000)
            self.assertEqual(calls, {'is_file': 12000, 'stat': 12000})
            identities = [video.id for video in app.videos]
            sequence = app.state['seq']
            app.scan()
            self.assertEqual(identities, [video.id for video in app.videos])
            self.assertEqual(app.state['seq'], sequence, 'Unchanged scan rewrites the entire asset journal')
            self.assertEqual(calls, {'is_file': 24000, 'stat': 24000})
            # 12000 completed annotations must not perform 12000 further SMB
            # roundtrips just to open the organize confirmation window.
            app.state['annotations'] = {video.id: {**app.empty_annotation(), 'label': 'fall', 'status': 'done'} for video in app.videos}
            with patch.object(Path, 'stat', side_effect=AssertionError('Plan accessed NAS')):
                self.assertEqual(len(app.export_plan()['items']), 12000)
        self.assertIn('建立视频索引', {values[0] for values in progress})
        self.assertIn('保存目录索引', {values[0] for values in progress})

    def test_auto_sample_does_not_scan_late_rows_but_manual_csv_does(self):
        (self.source / 'one.mp4').write_bytes(b'video')
        csv_path = self.source / 'index.csv'
        csv_path.write_text('file,device_id,label\n' + 'unrelated.mp4,OTHER,unknown\n' * 10020 + 'one.mp4,DEVICE,fall\n', encoding='utf-8')
        app = self.app()
        self.assertFalse(app.metadata_rows)
        self.assertIn('采样', app.metadata_warning)
        app.configure_metadata({'path': str(csv_path), 'fileColumn': 'file', 'deviceColumn': 'device_id', 'labelColumns': ['label']})
        self.assertEqual(app.videos[0].metadata_device_id, 'DEVICE')
        self.assertEqual(app.videos[0].metadata_labels, (('label', 'fall'),))

    def test_skip_during_full_csv_read_keeps_mapping_labels_and_loads_later(self):
        (self.source / 'one.mp4').write_bytes(b'video')
        csv_path = self.source / 'index.csv'
        csv_path.write_text('file,device_id,label\none.mp4,DEVICE,fall\n', encoding='utf-8')
        app = self.app()
        identity = app.videos[0].id
        app.save_annotation({'id': identity, 'revision': 0, 'annotation': {**app.empty_annotation(), 'label': 'fall', 'status': 'done'}})
        original_mapping = app.config['metadataConfig']
        app.close()
        skip = threading.Event()
        def progress(stage, *args):
            if stage == '读取原始标签':
                skip.set()
        # A slow CSV has enough rows for the periodic cancellation check.
        csv_path.write_text('file,device_id,label\n' + 'other.mp4,OTHER,unknown\n' * 300 + 'one.mp4,DEVICE,fall\n', encoding='utf-8')
        reopened = ProjectApp(self.source, {'_skip_metadata': skip}, progress)
        self.apps.append(reopened)
        self.assertTrue(reopened.metadata_deferred)
        self.assertEqual(reopened.config['metadataConfig'], original_mapping)
        self.assertEqual(reopened.annotation(identity)['label'], 'fall')
        self.assertIn('本次跳过', reopened.metadata_warning)
        reopened.configure_metadata({'path': str(csv_path), 'fileColumn': 'file', 'deviceColumn': 'device_id', 'labelColumns': ['label']})
        self.assertFalse(reopened.metadata_deferred)
        self.assertEqual(reopened.videos[0].metadata_device_id, 'DEVICE')

    def test_progress_reports_idle_time_and_optional_csv_skip(self):
        runtime = AppRuntime()
        runtime.report('匹配 CSV 字段', 12000, None, 'index.csv · CSV 行数')
        runtime.scan_updated -= 25
        status = runtime.snapshot()
        self.assertTrue(status['canSkipMetadata'])
        self.assertGreaterEqual(status['idleSeconds'], 25)
        runtime.skip_metadata_requested.set()
        self.assertFalse(runtime.snapshot()['canSkipMetadata'])


if __name__ == '__main__':
    unittest.main()
