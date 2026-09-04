#!/usr/bin/env python3
"""Local browser tool for quickly labeling whole videos and exporting Fall files."""

from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from reviewer import (
    VIDEO_EXTENSIONS, copy_file_bytes, ffmpeg_executable, ffprobe_duration,
    run_command, safe_json_write, user_cache_dir,
)


APP_DIR = Path(__file__).resolve().parent
INDEX_FILE = APP_DIR / "quick_label.html"
AUTO_CSV_EXCLUDES = {
    "clips.csv", "fall_export.csv", "no_fall_label_export.csv",
    "caregiver_fall_export.csv", "classification_manifest.csv",
}
FILE_COLUMN_PREFERENCES = (
    "blurred_file", "file", "filename", "file_name", "video_file",
    "video_filename", "video_path", "media_path", "path", "relative_path",
    "source", "output", "event_id", "review_id",
)
DEVICE_COLUMN_PREFERENCES = (
    "device_id", "deviceid", "device", "device_sn", "serial_number",
    "camera_id", "camera_sn",
)
LABEL_COLUMN_PREFERENCES = (
    "label", "manual_label", "human_label", "existing_human_label",
    "hitl_label", "truth_fall", "ground_truth", "target", "class",
    "online_yolo_alarm", "haochen_alarm", "fall_label", "prediction",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Video:
    id: str
    path: Path
    relative: str
    name: str
    size: int
    duration: float
    origin_label: str | None
    metadata_device_id: str
    metadata_matched: bool
    metadata_labels: tuple[tuple[str, str], ...]

    def public(self, label: str | None, duplicate_name: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "relative": self.relative,
            "size": self.size,
            "duration": self.duration,
            "label": label,
            "originLabel": self.origin_label,
            "metadataDeviceId": self.metadata_device_id,
            "metadataMatched": self.metadata_matched,
            "metadataLabels": [
                {"column": column, "value": value}
                for column, value in self.metadata_labels
            ],
            "duplicateName": duplicate_name,
        }


class LabelApp:
    def __init__(self, source: Path, output: Path, no_fall_output: Path, caregiver_output: Path, cache: Path) -> None:
        self.source = source.expanduser().resolve()
        self.output = output.expanduser().resolve()
        self.no_fall_output = no_fall_output.expanduser().resolve()
        self.caregiver_output = caregiver_output.expanduser().resolve()
        self.cache = cache.expanduser().resolve()
        if not self.source.is_dir():
            raise ValueError(f"源目录不存在：{self.source}")
        outputs = {self.output, self.no_fall_output, self.caregiver_output}
        if len(outputs) != 3:
            raise ValueError("跌倒、不跌倒与护工 Fall 归档目录必须互不相同")
        self.output.mkdir(parents=True, exist_ok=True)
        self.no_fall_output.mkdir(parents=True, exist_ok=True)
        self.caregiver_output.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.state_file = self._state_file_for_source()
        self.lock = threading.RLock()
        self.state = self._load_state()
        self.metadata_csv: Path | None = None
        self.metadata_config: dict[str, Any] = {}
        self.metadata_columns: list[str] = []
        self.metadata_rows: dict[str, dict[str, str]] = {}
        self.detected_csvs: list[Path] = []
        self.metadata_conflicts: set[str] = set()
        self.videos: list[Video] = []
        self.video_by_id: dict[str, Video] = {}
        self.proxy_jobs: dict[str, dict[str, Any]] = {}
        # Browser previews are short, but reviewers can advance faster than an
        # encode finishes. Keep conversion bounded so a run of quick labels
        # cannot create hundreds of simultaneous FFmpeg processes.
        self.proxy_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="label-preview")
        self.proxy_all_job: dict[str, Any] = {"status": "idle", "message": "尚未批量生成兼容预览"}
        self.export_job: dict[str, Any] = {"status": "idle", "message": "尚未归档"}
        self.scan()

    @staticmethod
    def _normalized_column(value: str) -> str:
        return "".join(character for character in value.casefold().strip() if character.isalnum() or character == "_")

    @classmethod
    def _preferred_column(cls, columns: list[str], preferences: tuple[str, ...]) -> str:
        normalized = {cls._normalized_column(column): column for column in columns}
        for preference in preferences:
            if preference in normalized:
                return normalized[preference]
        return ""

    @classmethod
    def _file_column_candidates(cls, columns: list[str]) -> list[str]:
        normalized = {cls._normalized_column(column): column for column in columns}
        candidates = [normalized[name] for name in FILE_COLUMN_PREFERENCES if name in normalized]
        candidates.extend(
            column for column in columns
            if any(token in cls._normalized_column(column) for token in ("file", "video", "media", "path"))
        )
        return list(dict.fromkeys(candidates))

    @classmethod
    def _guess_metadata_config(cls, path: Path, columns: list[str]) -> dict[str, Any]:
        file_candidates = cls._file_column_candidates(columns)
        file_column = file_candidates[0] if file_candidates else ""
        device_column = cls._preferred_column(columns, DEVICE_COLUMN_PREFERENCES)
        label_columns: list[str] = []
        normalized = {cls._normalized_column(column): column for column in columns}
        for preference in LABEL_COLUMN_PREFERENCES:
            column = normalized.get(preference)
            if column and column not in label_columns and column not in {file_column, device_column}:
                label_columns.append(column)
        if len(label_columns) < 3:
            for column in columns:
                name = cls._normalized_column(column)
                if column in label_columns or column in {file_column, device_column}:
                    continue
                if any(token in name for token in ("label", "truth", "alarm", "predict", "class")):
                    label_columns.append(column)
                if len(label_columns) == 3:
                    break
        return {
            "path": str(path), "fileColumn": file_column,
            "deviceColumn": device_column, "labelColumns": label_columns[:3],
        }

    @staticmethod
    def _read_csv_columns(path: Path) -> list[str]:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                return []
        return [str(column).strip() for column in header if str(column).strip()]

    def _discover_csvs(self) -> list[Path]:
        found: list[Path] = []
        seen: set[Path] = set()
        for directory in [self.source, *list(self.source.parents)[:8]]:
            try:
                candidates = sorted(
                    (item for item in directory.iterdir() if item.is_file() and item.suffix.casefold() == ".csv"),
                    key=lambda item: item.name.casefold(),
                )
            except (OSError, PermissionError):
                continue
            for candidate in candidates:
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved not in seen:
                    found.append(resolved)
                    seen.add(resolved)
        return found

    def _auto_metadata_config(self, video_keys: set[str]) -> dict[str, Any]:
        best: tuple[int, int, dict[str, Any]] | None = None
        for candidate in self.detected_csvs:
            if candidate.name.casefold() in AUTO_CSV_EXCLUDES:
                continue
            try:
                columns = self._read_csv_columns(candidate)
            except (OSError, csv.Error, UnicodeError):
                continue
            guessed = self._guess_metadata_config(candidate, columns)
            if guessed["fileColumn"]:
                file_candidates = self._file_column_candidates(columns)
                matches_by_column: dict[str, set[str]] = {column: set() for column in file_candidates}
                try:
                    with candidate.open(encoding="utf-8-sig", newline="") as handle:
                        for index, row in enumerate(csv.DictReader(handle)):
                            if index >= 100_000:
                                break
                            for column in file_candidates:
                                keys = self._metadata_keys(str(row.get(column, "")))
                                matches_by_column[column].update(key for key in keys if key in video_keys)
                except (OSError, csv.Error, UnicodeError):
                    continue
                best_file_column = max(file_candidates, key=lambda column: len(matches_by_column[column]))
                guessed["fileColumn"] = best_file_column
                matched = matches_by_column[best_file_column]
                name_bonus = 1 if candidate.name.casefold() in {"index.csv", "candidates.csv"} else 0
                score = (len(matched), name_bonus)
                if best is None or score > best[:2]:
                    best = (*score, guessed)
                if video_keys and len(matched) >= len(video_keys) // 2:
                    break
        return best[2] if best else {}

    @staticmethod
    def _metadata_keys(raw_value: str) -> tuple[str, ...]:
        filename = raw_value.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if not filename:
            return ()
        stem = Path(filename).stem.casefold()
        return tuple(dict.fromkeys((filename, stem)))

    def _load_metadata(self, video_keys: set[str]) -> None:
        self.metadata_csv = None
        self.metadata_columns = []
        self.metadata_rows = {}
        self.metadata_conflicts = set()
        self.detected_csvs = self._discover_csvs()
        saved = self.state.get("metadataConfig")
        config = dict(saved) if isinstance(saved, dict) else {}
        if config.get("disabled"):
            self.metadata_config = {"disabled": True}
            return
        configured_path = Path(str(config.get("path", ""))).expanduser() if config.get("path") else None
        if not configured_path or not configured_path.is_file():
            config = self._auto_metadata_config(video_keys)
        if not config:
            self.metadata_config = {}
            return
        path = Path(str(config.get("path", ""))).expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() != ".csv":
            self.metadata_config = {}
            return
        columns = self._read_csv_columns(path)
        guessed = self._guess_metadata_config(path, columns)
        file_column = str(config.get("fileColumn", ""))
        device_column = str(config.get("deviceColumn", ""))
        label_columns = [str(item) for item in config.get("labelColumns", []) if str(item)]
        if file_column not in columns:
            file_column = guessed["fileColumn"]
        if device_column not in columns:
            device_column = guessed["deviceColumn"]
        label_columns = [item for item in label_columns if item in columns and item not in {file_column, device_column}]
        if not label_columns:
            label_columns = guessed["labelColumns"]
        label_columns = list(dict.fromkeys(label_columns))[:3]
        if not file_column:
            self.metadata_config = {}
            return
        mapping: dict[str, dict[str, str]] = {}
        conflicts: set[str] = set()
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    clean_row = {str(key): str(value or "").strip() for key, value in row.items() if key is not None}
                    for key in self._metadata_keys(clean_row.get(file_column, "")):
                        previous = mapping.get(key)
                        if previous is not None and previous != clean_row:
                            conflicts.add(key)
                        else:
                            mapping[key] = clean_row
        except (OSError, csv.Error, UnicodeError):
            self.metadata_config = {}
            return
        for key in conflicts:
            mapping.pop(key, None)
        self.metadata_csv = path
        self.metadata_columns = columns
        self.metadata_rows = mapping
        self.metadata_conflicts = conflicts
        self.metadata_config = {
            "path": str(path), "fileColumn": file_column,
            "deviceColumn": device_column, "labelColumns": label_columns,
        }
        if self.state.get("metadataConfig") != self.metadata_config:
            self.state["metadataConfig"] = self.metadata_config
            self.save_state()

    def metadata_info(self) -> dict[str, Any]:
        return {
            "metadataCsv": str(self.metadata_csv) if self.metadata_csv else None,
            "metadataMatched": sum(video.metadata_matched for video in self.videos),
            "metadataConflicts": len(self.metadata_conflicts),
            "metadataConfig": self.metadata_config,
            "metadataColumns": self.metadata_columns,
            "detectedCsvs": [str(path) for path in self.detected_csvs],
        }

    def csv_columns(self, raw_path: str) -> dict[str, Any]:
        path = Path(raw_path.strip().strip("\"'")).expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() != ".csv":
            raise ValueError(f"CSV 文件不存在：{path}")
        columns = self._read_csv_columns(path)
        if not columns:
            raise ValueError("CSV 没有可用表头")
        return {"path": str(path), "columns": columns, "guess": self._guess_metadata_config(path, columns)}

    def csv_browser(self, raw_path: str) -> dict[str, Any]:
        path = Path(raw_path).expanduser() if raw_path.strip() else (self.metadata_csv.parent if self.metadata_csv else self.source)
        path = path.resolve()
        if not path.is_dir():
            raise ValueError(f"目录不存在：{path}")
        try:
            items = sorted(
                (item for item in path.iterdir() if not item.name.startswith(".")),
                key=lambda item: (not item.is_dir(), item.name.casefold()),
            )
        except PermissionError as exc:
            raise ValueError(f"没有权限读取目录：{path}") from exc
        directories = [{"name": item.name, "path": str(item)} for item in items if item.is_dir()]
        csv_files = [{"name": item.name, "path": str(item)} for item in items if item.is_file() and item.suffix.casefold() == ".csv"]
        parent = path.parent if path.parent != path else None
        return {
            "path": str(path), "parent": str(parent) if parent else None,
            "directories": directories[:1000], "csvFiles": csv_files[:1000],
            "truncated": len(directories) > 1000 or len(csv_files) > 1000,
        }

    def configure_metadata(self, payload: dict[str, Any]) -> None:
        if payload.get("disabled"):
            self.state["metadataConfig"] = {"disabled": True}
        elif payload.get("auto"):
            self.state.pop("metadataConfig", None)
        else:
            info = self.csv_columns(str(payload.get("path", "")))
            columns = info["columns"]
            file_column = str(payload.get("fileColumn", ""))
            device_column = str(payload.get("deviceColumn", ""))
            label_columns = [str(item) for item in payload.get("labelColumns", []) if str(item)]
            if file_column not in columns:
                raise ValueError("请选择用于匹配视频文件的 CSV 列")
            if device_column and device_column not in columns:
                raise ValueError("设备 ID 列不存在")
            if len(label_columns) > 3 or len(set(label_columns)) != len(label_columns):
                raise ValueError("展示标签最多选择 3 个，且不能重复")
            if any(item not in columns for item in label_columns):
                raise ValueError("选择的展示列不存在")
            if any(item in {file_column, device_column} for item in label_columns):
                raise ValueError("视频匹配列和设备 ID 列不能同时作为展示标签")
            self.state["metadataConfig"] = {
                "path": info["path"], "fileColumn": file_column,
                "deviceColumn": device_column, "labelColumns": label_columns,
            }
        self.save_state()
        self.scan()

    def _state_file_for_source(self) -> Path:
        primary = self.output / ".fall_label_state.json"
        source_key = hashlib.sha256(str(self.source).encode("utf-8")).hexdigest()[:12]
        scoped = self.output / f".fall_label_state_{source_key}.json"
        if scoped.exists() or not primary.exists():
            return scoped if scoped.exists() else primary
        try:
            saved_source = json.loads(primary.read_text(encoding="utf-8")).get("source")
        except (OSError, json.JSONDecodeError, AttributeError):
            saved_source = None
        return primary if saved_source == str(self.source) else scoped

    def _load_state(self) -> dict[str, Any]:
        empty = {"source": str(self.source), "labels": {}}
        if not self.state_file.exists():
            return empty
        try:
            loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if loaded.get("source") != str(self.source):
            return empty
        loaded.setdefault("labels", {})
        return loaded

    def save_state(self) -> None:
        with self.lock:
            safe_json_write(self.state_file, self.state)

    def scan(self) -> None:
        categories = [
            (self.output, "fall", "跌倒"),
            (self.no_fall_output, "no_fall", "不跌倒"),
            (self.caregiver_output, "caregiver_fall", "护工 Fall"),
        ]
        category_by_path = {directory: (label, display) for directory, label, display in categories}
        scan_roots = [(self.source, *category_by_path.get(self.source, (None, "未分类")))]
        scan_roots.extend((directory, label, display) for directory, label, display in categories if directory != self.source)
        paths: list[tuple[Path, str | None, str]] = []
        for directory, origin_label, display in scan_roots:
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                paths.append((path, origin_label, display))
        paths.sort(key=lambda item: (item[1] or "", item[0].name.casefold()))
        video_keys = {
            key
            for path, _origin_label, _display in paths
            for key in (path.name.casefold(), path.stem.casefold())
        }
        self._load_metadata(video_keys)
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(paths)))) as pool:
            durations = list(pool.map(ffprobe_duration, (item[0] for item in paths)))
        videos: list[Video] = []
        for (path, origin_label, display), duration in zip(paths, durations):
            if origin_label is None:
                relative = path.relative_to(self.source).as_posix()
                identity = relative  # Preserve IDs and progress from earlier versions.
            else:
                relative = f"[{display}] {path.name}"
                identity = f"category:{origin_label}:{path.name}"
            identifier = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
            row = self.metadata_rows.get(path.name.casefold()) or self.metadata_rows.get(path.stem.casefold())
            device_column = str(self.metadata_config.get("deviceColumn", ""))
            label_columns = [str(item) for item in self.metadata_config.get("labelColumns", [])]
            videos.append(Video(
                identifier, path.resolve(), relative, path.name,
                path.stat().st_size, duration, origin_label,
                str(row.get(device_column, "")) if row and device_column else "",
                row is not None,
                tuple((column, str(row.get(column, "")) if row else "") for column in label_columns),
            ))
        with self.lock:
            self.videos = videos
            self.video_by_id = {video.id: video for video in videos}

    def public_videos(self) -> list[dict[str, Any]]:
        names: dict[str, int] = {}
        for video in self.videos:
            names[video.name.casefold()] = names.get(video.name.casefold(), 0) + 1
        labels = self.state["labels"]
        return [
            video.public(labels[video.id] if video.id in labels else video.origin_label, names[video.name.casefold()] > 1)
            for video in self.videos
        ]

    def public_labels(self) -> dict[str, str]:
        labels = self.state["labels"]
        return {
            video.id: labels[video.id] if video.id in labels else video.origin_label
            for video in self.videos
            if (labels[video.id] if video.id in labels else video.origin_label) is not None
        }

    def get_video(self, video_id: str) -> Video:
        try:
            return self.video_by_id[video_id]
        except KeyError as exc:
            raise ValueError("视频不存在或列表已经变化") from exc

    def set_label(self, video_id: str, label: str | None) -> None:
        self.get_video(video_id)
        if label not in {"fall", "no_fall", "caregiver_fall", None}:
            raise ValueError("标签只能是 fall、no_fall、caregiver_fall 或空")
        with self.lock:
            if label is None:
                self.state["labels"].pop(video_id, None)
            else:
                self.state["labels"][video_id] = label
            self.save_state()

    def proxy_path(self, video_id: str) -> Path:
        video = self.get_video(video_id)
        try:
            modified_ns = video.path.stat().st_mtime_ns
        except OSError:
            modified_ns = 0
        fingerprint = hashlib.sha256(
            f"{self.source}\0{video.path}\0{video.size}\0{modified_ns}".encode("utf-8")
        ).hexdigest()[:20]
        return self.cache / f"label-{fingerprint}.mp4"

    def proxy_status(self, video_id: str) -> dict[str, Any]:
        path = self.proxy_path(video_id)
        if path.exists() and path.stat().st_size > 0:
            return {"status": "ready", "url": f"/proxy/{quote(video_id)}"}
        with self.lock:
            return dict(self.proxy_jobs.get(video_id, {"status": "idle"}))

    def _claim_proxy(self, video_id: str) -> str:
        self.get_video(video_id)
        if self.proxy_path(video_id).is_file() and self.proxy_path(video_id).stat().st_size > 0:
            return "ready"
        with self.lock:
            if self.proxy_jobs.get(video_id, {}).get("status") == "running":
                return "running"
            self.proxy_jobs[video_id] = {"status": "running", "message": "正在生成兼容预览…"}
            return "claimed"

    def start_proxy(self, video_id: str) -> dict[str, Any]:
        claimed = self._claim_proxy(video_id)
        if claimed == "claimed":
            self.proxy_executor.submit(self._make_proxy, video_id)
        return self.proxy_status(video_id)

    def _make_proxy(self, video_id: str) -> None:
        temporary: Path | None = None
        try:
            video = self.get_video(video_id)
            destination = self.proxy_path(video_id)
            temporary = destination.with_suffix(".tmp.mp4")

            def command(include_audio: bool) -> list[str]:
                audio = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k"] if include_audio else ["-an"]
                return [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-fflags", "+genpts", "-i", str(video.path), "-map", "0:v:0", *audio,
                    "-vf", "scale='min(1280,iw)':-2:flags=lanczos,format=yuv420p,pad='ceil(iw/2)*2':'ceil(ih/2)*2'",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-profile:v", "main", "-tag:v", "avc1", "-sn", "-dn",
                    "-max_muxing_queue_size", "2048", "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart", str(temporary),
                ]

            result = run_command(command(include_audio=True))
            if result.returncode != 0:
                temporary.unlink(missing_ok=True)
                result = run_command(command(include_audio=False))
            with self.lock:
                if result.returncode == 0 and temporary.exists() and temporary.stat().st_size > 0:
                    temporary.replace(destination)
                    self.proxy_jobs[video_id] = {
                        "status": "ready", "url": f"/proxy/{quote(video_id)}",
                        "message": "兼容预览已生成；归档仍移动原视频且不转码",
                    }
                else:
                    temporary.unlink(missing_ok=True)
                    self.proxy_jobs[video_id] = {
                        "status": "error", "message": result.stderr.strip()[-1000:] or "生成预览失败",
                    }
        except Exception as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            with self.lock:
                self.proxy_jobs[video_id] = {"status": "error", "message": str(exc)}

    def start_proxy_all(self) -> dict[str, Any]:
        with self.lock:
            if self.proxy_all_job.get("status") == "running":
                return dict(self.proxy_all_job)
            self.proxy_all_job = {
                "status": "running", "done": 0, "total": len(self.videos),
                "ready": 0, "failed": 0, "message": "正在准备全部兼容预览…",
            }
        threading.Thread(target=self._make_all_proxies, daemon=True).start()
        return dict(self.proxy_all_job)

    def _make_all_proxies(self) -> None:
        video_ids = [video.id for video in self.videos]
        ready = failed = 0
        for index, video_id in enumerate(video_ids, start=1):
            try:
                claimed = self._claim_proxy(video_id)
                if claimed == "claimed":
                    self._make_proxy(video_id)
                elif claimed == "running":
                    for _ in range(1200):
                        if self.proxy_status(video_id).get("status") != "running":
                            break
                        time.sleep(0.1)
                if self.proxy_status(video_id).get("status") == "ready":
                    ready += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            with self.lock:
                self.proxy_all_job.update(
                    done=index, ready=ready, failed=failed,
                    message=f"正在生成兼容预览 {index}/{len(video_ids)}；成功 {ready}，失败 {failed}",
                )
        with self.lock:
            self.proxy_all_job.update(
                status="done" if failed == 0 else "error",
                message=f"兼容预览完成：成功 {ready} 个，失败 {failed} 个。原视频未修改。",
            )

    def start_export(self) -> dict[str, Any]:
        with self.lock:
            if self.export_job.get("status") == "running":
                return dict(self.export_job)
            self.export_job = {"status": "running", "done": 0, "total": 0, "message": "正在检查…"}
        threading.Thread(target=self._archive_labeled, daemon=True).start()
        return dict(self.export_job)

    def _move_original(self, video: Video, destination: Path) -> None:
        if destination.exists():
            if destination.stat().st_size != video.size or sha256_file(destination) != sha256_file(video.path):
                raise RuntimeError(f"已有同名文件但内容不同，未覆盖：{video.name}")
            video.path.unlink()
            return
        try:
            video.path.replace(destination)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
        temporary = destination.parent / f".{video.name}.archive-{os.getpid()}-{time.time_ns()}.tmp"
        try:
            copy_file_bytes(video.path, temporary)
            if temporary.stat().st_size != video.size or sha256_file(temporary) != sha256_file(video.path):
                raise RuntimeError(f"跨磁盘移动校验失败：{video.name}")
            temporary.replace(destination)
            video.path.unlink()
        finally:
            temporary.unlink(missing_ok=True)

    def _archive_labeled(self) -> None:
        destinations = {
            "fall": (self.output, "跌倒"),
            "no_fall": (self.no_fall_output, "不跌倒"),
            "caregiver_fall": (self.caregiver_output, "护工 Fall"),
        }
        try:
            labels = self.state["labels"]
            labeled = [
                (video, labels[video.id] if video.id in labels else video.origin_label)
                for video in self.videos
            ]
            plan = [
                (video, label) for video, label in labeled
                if label in destinations and video.path.parent != destinations[label][0]
            ]
            if not plan:
                with self.lock:
                    self.export_job.update(
                        status="done", done=0, total=0,
                        message="检查完成：所有已分类视频都在正确目录中，无需移动。",
                    )
                return
            total = len(plan)
            succeeded = 0
            failures: list[str] = []
            with self.lock:
                self.export_job.update(total=total, message=f"准备归档 {total} 个已标记原视频")
            for index, (video, label) in enumerate(plan, start=1):
                output, display = destinations[label]
                destination = output / video.name
                with self.lock:
                    self.export_job["message"] = f"正在移动 {index}/{total}（{display}）：{video.name}"
                try:
                    self._move_original(video, destination)
                    succeeded += 1
                except Exception as exc:
                    failures.append(f"{video.name}：{exc}")
                with self.lock:
                    self.export_job["done"] = index

            self.scan()
            with self.lock:
                if failures:
                    self.export_job.update(
                        status="error", done=total, total=total,
                        message=f"部分完成：成功移动 {succeeded} 个，失败 {len(failures)} 个；失败视频仍留在原目录。\n" + "\n".join(failures[:10]),
                    )
                else:
                    self.export_job.update(
                        status="done", done=total, total=total,
                        message=f"完成：已将 {succeeded} 个视频整理到正确分类目录；未标记视频仍在原目录。",
                    )
        except Exception as exc:
            with self.lock:
                self.export_job.update(status="error", message=str(exc))


class LabelHandler(BaseHTTPRequestHandler):
    server: "LabelServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith(("/media/", "/proxy/")):
            return
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求数据过大")
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                self._send_file(INDEX_FILE, allow_range=False)
            elif parsed.path == "/api/videos":
                self.send_json({
                    "source": str(self.server.app.source), "output": str(self.server.app.output),
                    "noFallOutput": str(self.server.app.no_fall_output),
                    "caregiverOutput": str(self.server.app.caregiver_output),
                    **self.server.app.metadata_info(),
                    "videos": self.server.app.public_videos(),
                })
            elif parsed.path == "/api/state":
                with self.server.app.lock:
                    self.send_json({"labels": self.server.app.public_labels()})
            elif parsed.path == "/api/export-status":
                with self.server.app.lock:
                    self.send_json(dict(self.server.app.export_job))
            elif parsed.path == "/api/proxy-all-status":
                with self.server.app.lock:
                    self.send_json(dict(self.server.app.proxy_all_job))
            elif parsed.path == "/api/proxy-status":
                self.send_json(self.server.app.proxy_status(query.get("id", [""])[0]))
            elif parsed.path == "/api/csv-columns":
                self.send_json(self.server.app.csv_columns(query.get("path", [""])[0]))
            elif parsed.path == "/api/csv-browser":
                self.send_json(self.server.app.csv_browser(query.get("path", [""])[0]))
            elif parsed.path.startswith("/media/"):
                self._send_file(self.server.app.get_video(parsed.path.removeprefix("/media/")).path, allow_range=True)
            elif parsed.path.startswith("/proxy/"):
                video_id = parsed.path.removeprefix("/proxy/")
                self.server.app.get_video(video_id)
                self._send_file(self.server.app.proxy_path(video_id), allow_range=True)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, OSError, csv.Error, UnicodeError, subprocess.TimeoutExpired) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            data = self.read_json()
            if parsed.path == "/api/label":
                self.server.app.set_label(str(data.get("id", "")), data.get("label"))
                self.send_json({"ok": True})
            elif parsed.path == "/api/export":
                self.send_json(self.server.app.start_export())
            elif parsed.path == "/api/rescan":
                self.server.app.scan()
                self.send_json({
                    "ok": True,
                    **self.server.app.metadata_info(),
                    "videos": self.server.app.public_videos(),
                })
            elif parsed.path == "/api/proxy":
                self.send_json(self.server.app.start_proxy(str(data.get("id", ""))))
            elif parsed.path == "/api/proxy-all":
                self.send_json(self.server.app.start_proxy_all())
            elif parsed.path == "/api/metadata-config":
                self.server.app.configure_metadata(data)
                self.send_json({
                    "ok": True, **self.server.app.metadata_info(),
                    "videos": self.server.app.public_videos(),
                })
            elif parsed.path == "/api/shutdown":
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, OSError, csv.Error, UnicodeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_HEAD(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_file(INDEX_FILE, allow_range=False, send_body=False)
            elif parsed.path.startswith("/media/"):
                self._send_file(self.server.app.get_video(parsed.path.removeprefix("/media/")).path, allow_range=True, send_body=False)
            elif parsed.path.startswith("/proxy/"):
                video_id = parsed.path.removeprefix("/proxy/")
                self.server.app.get_video(video_id)
                self._send_file(self.server.app.proxy_path(video_id), allow_range=True, send_body=False)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def _send_file(self, path: Path, allow_range: bool, send_body: bool = True) -> None:
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, HTTPStatus.OK
        range_header = self.headers.get("Range") if allow_range else None
        if range_header and range_header.startswith("bytes="):
            requested = range_header[6:].split(",", 1)[0]
            first, _, last = requested.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else end
                elif last:
                    start = max(0, size - int(last))
            except ValueError:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            if start < 0 or start >= size or end < start:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            end, status = min(end, size - 1), HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body:
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


class LabelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: LabelApp) -> None:
        super().__init__(address, LabelHandler)
        self.app = app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="快速标记整段 Fall 视频并原样导出")
    parser.add_argument("--source", required=True, help="未分类/项目目录；快速分类会联合扫描它和三个分类目录的第一层")
    parser.add_argument("--output", help="Fall 视频输出目录；默认是项目目录/fall_output")
    parser.add_argument("--no-fall-output", help="不跌倒视频输出目录；默认是项目目录/no_fall_output")
    parser.add_argument("--caregiver-output", help="护工 Fall 视频输出目录；默认是项目目录/caregiver_fall_output")
    parser.add_argument("--cache", default=str(user_cache_dir("label-preview")), help="兼容预览缓存")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        ffmpeg_executable()
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    source = Path(args.source).expanduser()
    if not source.is_dir():
        print(f"错误：项目目录不存在：{source}", file=sys.stderr)
        return 2
    output = Path(args.output).expanduser() if args.output else source / "fall_output"
    no_fall_output = Path(args.no_fall_output).expanduser() if args.no_fall_output else source / "no_fall_output"
    caregiver_output = Path(args.caregiver_output).expanduser() if args.caregiver_output else source / "caregiver_fall_output"
    print(f"正在联合扫描项目与三个分类目录：{source.resolve()}", flush=True)
    app = LabelApp(source, output, no_fall_output, caregiver_output, Path(args.cache))
    server = LabelServer((args.host, args.port), app)
    url = f"http://{args.host}:{args.port}"
    print(f"已找到 {len(app.videos)} 个视频")
    print(f"项目目录：{app.source}")
    print(f"Fall 输出目录：{app.output}")
    print(f"不跌倒输出目录：{app.no_fall_output}")
    print(f"护工 Fall 输出目录：{app.caregiver_output}")
    print(f"审核页面：{url}")
    print("按 Ctrl+C 停止工具")
    if not args.no_browser and not os.environ.get("SSH_CONNECTION"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n工具已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
