from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quick_labeler import LabelApp


class QuickLabelMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "project"
        self.source.mkdir()
        self.video_name = "example-event.mp4"
        (self.source / self.video_name).write_bytes(b"test-video")
        with (self.source / "index.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "event_id", "device_id", "blurred_file", "hitl_label",
                    "online_yolo_alarm", "haochen_alarm", "score",
                ],
            )
            writer.writeheader()
            writer.writerow({
                "event_id": "example-event", "device_id": "DEVICE-001",
                "blurred_file": self.video_name, "hitl_label": "0",
                "online_yolo_alarm": "1", "haochen_alarm": "0", "score": "0.42",
            })
        duration_patch = patch("quick_labeler.ffprobe_duration", return_value=8.0)
        duration_patch.start()
        self.addCleanup(duration_patch.stop)
        self.app = LabelApp(
            self.source, self.root / "fall", self.root / "no_fall",
            self.root / "caregiver", self.root / "cache",
        )
        self.addCleanup(lambda: self.app.proxy_executor.shutdown(wait=False, cancel_futures=True))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_auto_detects_file_device_and_three_label_columns(self) -> None:
        info = self.app.metadata_info()
        video = self.app.public_videos()[0]
        self.assertEqual(info["metadataConfig"]["fileColumn"], "blurred_file")
        self.assertEqual(info["metadataConfig"]["deviceColumn"], "device_id")
        self.assertEqual(info["metadataMatched"], 1)
        self.assertEqual(video["metadataDeviceId"], "DEVICE-001")
        self.assertEqual(
            [item["column"] for item in video["metadataLabels"]],
            ["hitl_label", "online_yolo_alarm", "haochen_alarm"],
        )

    def test_manual_selection_is_limited_to_three_unique_labels(self) -> None:
        with self.assertRaisesRegex(ValueError, "最多选择 3 个"):
            self.app.configure_metadata({
                "path": str(self.source / "index.csv"),
                "fileColumn": "blurred_file", "deviceColumn": "device_id",
                "labelColumns": ["hitl_label", "online_yolo_alarm", "haochen_alarm", "score"],
            })

    def test_auto_detection_chooses_the_column_that_matches_video_names(self) -> None:
        alternate = self.source / "alternate.csv"
        with alternate.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["blurred_file", "file", "device_id", "label"])
            writer.writeheader()
            writer.writerow({
                "blurred_file": "different-video.mp4", "file": self.video_name,
                "device_id": "DEVICE-002", "label": "candidate",
            })
        (self.source / "index.csv").unlink()
        self.app.state.pop("metadataConfig", None)
        self.app.scan()
        self.assertEqual(self.app.metadata_config["fileColumn"], "file")
        self.assertEqual(self.app.public_videos()[0]["metadataDeviceId"], "DEVICE-002")

    def test_archive_moves_original_without_creating_export_csv(self) -> None:
        video = self.app.videos[0]
        self.app.set_label(video.id, "fall")
        self.app._archive_labeled()
        self.assertTrue((self.root / "fall" / self.video_name).is_file())
        self.assertEqual(
            [path.name for path in self.root.rglob("*.csv")],
            ["index.csv"],
        )
        self.assertTrue((self.root / "fall" / ".fall_label_state.json").is_file())


if __name__ == "__main__":
    unittest.main()
