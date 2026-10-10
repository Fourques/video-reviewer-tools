from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_app import ProjectApp
from project_schema import preset, validate
from project_store import ConflictError, ProjectStore


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "project"
        self.source.mkdir()
        (self.source / "one.mp4").write_bytes(b"video-one")
        (self.source / "two.mp4").write_bytes(b"video-two")
        self.apps = []
        self.cache_patch = patch("project_app.user_cache_dir", return_value=self.root / "cache")
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def tearDown(self):
        for app in self.apps:
            app.close()
        self.temporary.cleanup()

    def app(self, **selection):
        app = ProjectApp(self.source, selection)
        self.apps.append(app)
        return app

    def save(self, app, video=None, **values):
        video = video or app.videos[0]
        current = app.annotation(video.id)
        return app.save_annotation({"id": video.id, "revision": current["revision"], "token": str(current["revision"]), "annotation": {**current, **values}})

    def test_dynamic_labels_and_display_rename_keep_progress(self):
        app = self.app(preset="custom")
        config = {**app.config, "labels": [{"id": "different", "name": "扶起", "description": "", "color": "#aabbcc", "key": "j", "folder": "help", "active": True}]}
        app.configure({"seq": app.state["seq"], "config": config})
        self.save(app, label="different", status="done")
        config["labels"][0]["name"] = "搀扶"
        app.configure({"seq": app.state["seq"], "config": config})
        self.assertEqual(app.public_videos()[0]["annotation"]["label"], "different")
        self.assertEqual(app.config["labels"][0]["name"], "搀扶")

    def test_used_label_cannot_be_deleted(self):
        app = self.app()
        self.save(app, label="fall", status="done")
        config = {**app.config, "labels": app.config["labels"][1:]}
        with self.assertRaises(ValueError):
            app.configure({"seq": app.state["seq"], "config": config})

    def test_stale_config_cannot_overwrite_new_rules(self):
        app = self.app()
        revision = app.config.get('revision', 0)
        app.configure({'seq': app.state['seq'], 'configRevision': revision, 'config': {**app.config, 'name': 'new name'}})
        with self.assertRaises(ConflictError):
            app.configure({'seq': app.state['seq'], 'configRevision': revision, 'config': {**app.config, 'name': 'stale name'}})
        self.assertEqual(app.config['name'], 'new name')

    def test_empty_is_not_no_fall_and_review_does_not_export(self):
        app = self.app()
        with self.assertRaises(ValueError):
            self.save(app, status="done")
        self.save(app, label="fall", status="review")
        self.assertFalse(app.export_plan()["items"])

    def test_revision_conflict_and_idempotent_retries(self):
        app = self.app()
        video = app.videos[0]
        payload = {"id": video.id, "revision": 0, "token": "same", "annotation": {**app.empty_annotation(), "label": "fall", "status": "done"}}
        first = app.save_annotation(payload)
        self.assertEqual(app.save_annotation(payload), first)
        payload["token"] = "different"
        with self.assertRaises(ConflictError):
            app.save_annotation(payload)

    def test_multiple_undo_keeps_older_history_usable(self):
        app = self.app()
        self.save(app, label="fall")
        self.save(app, label="no_fall")
        app.undo()
        self.assertEqual(app.annotation(app.videos[0].id)["label"], "fall")
        app.undo()
        self.assertIsNone(app.annotation(app.videos[0].id)["label"])

    def test_migrate_v1_and_keep_legacy_file(self):
        directory = self.source / "fall_output"
        directory.mkdir()
        path = directory / ".fall_label_state.json"
        legacy = {"source": "/different/mount/project", "labels": {hashlib.sha256(b"one.mp4").hexdigest()[:20]: "caregiver_fall"}}
        path.write_text(json.dumps(legacy))
        original = path.read_bytes()
        app = self.app()
        self.assertEqual(app.annotation(app.videos[0].id)["label"], "caregiver_fall")
        self.assertEqual(app.migration["imported"], 1)
        self.assertEqual(path.read_bytes(), original)

    def test_category_is_prior_not_completed_and_new_round_preserves_labels(self):
        directory = self.source / "fall_output"
        directory.mkdir()
        (self.source / "one.mp4").replace(directory / "one.mp4")
        app = self.app()
        video = next(video for video in app.videos if video.name == "one.mp4")
        self.assertEqual(video.origin_label, "fall")
        self.assertEqual(app.annotation(video.id)["status"], "pending")
        self.save(app, video, label="fall", status="done")
        app.store.commit({"round": 2})
        self.assertEqual(app.annotation(video.id)["status"], "pending")
        self.assertEqual(app.annotation(video.id)["label"], "fall")

    def test_only_first_directory_level_is_scanned(self):
        child = self.source / "child"
        child.mkdir()
        (child / "hidden.mp4").write_bytes(b"hidden")
        self.assertEqual(len(self.app().videos), 2)

    def test_single_writer_and_crash_lock_release(self):
        app = self.app()
        with self.assertRaises(ValueError):
            ProjectApp(self.source)
        app.close()
        self.assertEqual(len(self.app().videos), 2)

    def test_move_preserves_asset_identity_and_does_not_create_csv(self):
        app = self.app()
        identity = app.videos[0].id
        self.save(app, label="fall", status="done")
        app._export(app.export_plan()["items"])
        self.assertEqual(app.export_job["status"], "done")
        self.assertEqual(app.get_video(identity).path.parent, self.source / "output/fall")
        app.scan()
        self.assertIn(identity, app.video_by_id)
        self.assertFalse(list(self.source.rglob("*.csv")))
        self.assertFalse(app.export_plan()["items"])

    def test_output_change_does_not_hide_previous_project_members(self):
        app = self.app()
        identity = app.videos[0].id
        self.save(app, label='fall', status='done')
        app._export(app.export_plan()['items'])
        app.configure({'seq': app.state['seq'], 'config': {**app.config, 'output': 'new-output'}})
        self.assertIn(identity, app.video_by_id)
        app._export(app.export_plan()['items'])
        self.assertEqual(app.get_video(identity).path, self.source / 'new-output/fall/one.mp4')

    def test_unavailable_extra_mount_can_be_rebound_in_settings(self):
        app = self.app()
        app.config['inputs'] = ['unavailable-mount']
        app.scan()
        self.assertEqual(len(app.videos), 2)
        self.assertTrue(app.document()['inputWarnings'])

    def test_copy_retry_does_not_duplicate_successful_outputs(self):
        app = self.app()
        app.config["exportMode"] = "copy"
        self.save(app, label="fall", status="done")
        app._export(app.export_plan()["items"])
        self.assertTrue((self.source / "one.mp4").exists())
        self.assertFalse(app.export_plan()["items"])

    def test_network_disk_without_hardlinks_can_move(self):
        app = self.app()
        self.save(app, label='fall', status='done')
        with patch('project_app.os.link', side_effect=OSError('operation not supported')):
            app._export(app.export_plan()['items'])
        self.assertEqual(app.export_job['status'], 'done')
        self.assertEqual((self.source / 'output/fall/one.mp4').read_bytes(), b'video-one')

    def test_publish_never_overwrites_a_concurrent_destination(self):
        original = self.source / 'one.mp4'
        target = self.source / 'existing.mp4'
        target.write_bytes(b'external file')
        with patch('project_app.os.link', side_effect=OSError('not supported')):
            with self.assertRaises(FileExistsError):
                ProjectApp._publish_original(original, target, hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertEqual(target.read_bytes(), b'external file')

    def test_changed_source_is_not_exported(self):
        app = self.app()
        self.save(app, label='fall', status='done')
        plan = app.export_plan()
        (self.source / 'one.mp4').write_bytes(b'replaced video')
        app._export(plan['items'])
        self.assertEqual(app.export_job['status'], 'error')
        self.assertTrue((self.source / 'one.mp4').exists())

    def test_modified_copy_receipt_does_not_hide_a_conflict(self):
        app = self.app()
        app.config['exportMode'] = 'copy'
        self.save(app, label='fall', status='done')
        app._export(app.export_plan()['items'])
        (self.source / 'output/fall/one.mp4').write_bytes(b'changed by another tool')
        plan = app.export_plan()
        self.assertEqual(len(plan['checks']), 1)
        app._export(plan['checks'])
        self.assertIn('被修改', app.export_job['failures'][0]['error'])
        self.assertEqual((self.source / 'output/fall/one.mp4').read_bytes(), b'changed by another tool')
        self.assertTrue((self.source / 'one.mp4').exists())

    def test_missing_csv_mapping_is_preserved(self):
        app = self.app()
        saved = {'path': str(self.source / 'missing.csv'), 'fileColumn': 'file', 'deviceColumn': 'id', 'labelColumns': []}
        app.store.commit({'metadataConfig': saved})
        app.scan()
        self.assertEqual(app.metadata_config, saved)
        self.assertTrue(app.metadata_warning)

    def test_clip_review_without_segments_migrates_to_review_not_negative(self):
        output = self.source / 'output'
        output.mkdir()
        identity = hashlib.sha256(b'one.mp4').hexdigest()[:20]
        (output / '.clip_reviewer_state.json').write_text(json.dumps({'source': str(self.source), 'reviewed': {identity: True}}))
        app = self.app(preset='clips')
        self.assertEqual(app.annotation(app.videos[0].id)['status'], 'review')
        self.assertIsNone(app.annotation(app.videos[0].id)['label'])

    def test_one_export_failure_does_not_block_next(self):
        app = self.app()
        for video in app.videos:
            self.save(app, video, label="fall", status="done")
        directory = self.source / "output/fall"
        directory.mkdir(parents=True)
        (directory / "one.mp4").write_bytes(b"conflict")
        app._export(app.export_plan()["items"])
        self.assertEqual(len(app.export_job["failures"]), 1)
        self.assertTrue((directory / "two.mp4").exists())
        self.assertTrue((self.source / "one.mp4").exists())

    def test_relocated_project_keeps_progress(self):
        app = self.app()
        identity = app.videos[0].id
        self.save(app, label="fall", status="done")
        app.close()
        new = self.root / "relocated"
        self.source.replace(new)
        self.source = new
        reopened = self.app()
        self.assertIn(identity, reopened.video_by_id)
        self.assertEqual(reopened.annotation(identity)["status"], "done")

    def test_corrupt_progress_is_never_silently_reset(self):
        directory = self.source / ".video-reviewer"
        directory.mkdir()
        path = directory / "state.json"
        path.write_bytes(b"not-json")
        with self.assertRaises(ValueError):
            self.app()
        self.assertEqual(path.read_bytes(), b"not-json")

    def test_arbitrary_segment_duration_and_nan_rejected(self):
        app = self.app(preset="clips")
        segment = {"id": "seg", "start": 1, "end": 5, "label": "fall"}
        with patch.object(app, "video_info", return_value={"duration": 10, "keyframes": [0, 1]}):
            self.save(app, segments=[segment], status="done")
            self.assertEqual(app.export_plan()["items"][0]["segment"]["end"], 5)
            with self.assertRaises(ValueError):
                    self.save(app, segments=[{**segment, "start": float("nan")}])

    def test_export_with_preview_snap_off_still_uses_selected_region(self):
        app = self.app(preset='clips')
        item = {'segment': {'start': 5.2, 'end': 9.2}}
        from types import SimpleNamespace
        with patch.object(app, 'video_info', return_value={'keyframes': [], 'duration': 12}), patch('project_app.ffprobe_keyframes', return_value=[0, 5, 10]), patch('project_app.ffprobe_duration', return_value=4), patch('project_app.run_command', return_value=SimpleNamespace(returncode=0)) as command:
            result = app._clip(app.videos[0], item, self.root / 'temporary.mp4')
        self.assertEqual(result['actualStart'], 5)
        self.assertEqual(command.call_args.args[0][command.call_args.args[0].index('-ss') + 1], '5.000000')


class StoreTests(unittest.TestCase):
    def test_journal_recovers_acknowledged_data_and_incomplete_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root)
            store.commit(merge={"annotations": {"a": {"label": "saved"}}})
            store.handle.close()  # simulate killed process without snapshot
            store.closed = True
            with (root / "events.jsonl").open("ab") as handle:
                handle.write(b'{"seq":2')
            reopened = ProjectStore(root)
            self.assertEqual(reopened.state["annotations"]["a"]["label"], "saved")
            self.assertIn("半条", reopened.warning)
            reopened.commit({"cursor": "a"})
            reopened.close()
            final = ProjectStore(root)
            self.assertEqual(final.state["cursor"], "a")
            final.close()

    def test_failed_durable_write_does_not_change_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            with patch("project_store.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.commit({"cursor": "bad"})
            self.assertIsNone(store.state["cursor"])
            store.commit({"cursor": "good"})
            store.close()

    def test_invalid_schema_and_unsafe_output_names(self):
        for folder in ("../other", "CON", "nul.mp4", "name."):
            config = preset()
            config["labels"][0]["folder"] = folder
            with self.assertRaises(ValueError):
                validate(config)
        config = preset()
        config["labels"][0]["key"] = "s"
        with self.assertRaises(ValueError):
            validate(config)


if __name__ == "__main__":
    unittest.main()
