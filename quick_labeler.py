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
        self.device_by_filename: dict[str, str] = {}
        self.metadata_conflicts: set[str] = set()
        self.videos: list[Video] = []
        self.video_by_id: dict[str, Video] = {}
        self.proxy_jobs: dict[str, dict[str, Any]] = {}
        # Browser previews are short, but reviewers can advance faster than an
        # encode finishes. Keep conversion bounded so a run of quick labels
        # cannot create hundreds of simultaneous FFmpeg processes.
        self.proxy_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="label-preview")
        self.export_job: dict[str, Any] = {"status": "idle", "message": "尚未归档"}
        self.scan()

    def _load_device_metadata(self) -> None:
        self.metadata_csv = None
        self.device_by_filename = {}
        self.metadata_conflicts = set()
        search_dirs = [self.source, *list(self.source.parents)[:8]]
        for directory in search_dirs:
            candidate = directory / "candidates.csv"
            if not candidate.is_file():
                continue
            try:
                with candidate.open(encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if not reader.fieldnames or not {"file", "device_id"}.issubset(reader.fieldnames):
                        continue
                    mapping: dict[str, str] = {}
                    conflicts: set[str] = set()
                    for row in reader:
                        raw_file = str(row.get("file", "")).strip()
                        device_id = str(row.get("device_id", "")).strip()
                        if not raw_file or not device_id:
                            continue
                        filename = raw_file.replace("\\", "/").rsplit("/", 1)[-1].casefold()
                        previous = mapping.get(filename)
                        if previous is not None and previous != device_id:
                            conflicts.add(filename)
                        else:
                            mapping[filename] = device_id
                    for filename in conflicts:
                        mapping.pop(filename, None)
            except (OSError, csv.Error, UnicodeError):
                continue
            if mapping:
                self.metadata_csv = candidate.resolve()
                self.device_by_filename = mapping
                self.metadata_conflicts = conflicts
                return

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
        self._load_device_metadata()
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
            videos.append(Video(
                identifier, path.resolve(), relative, path.name,
                path.stat().st_size, duration, origin_label,
                self.device_by_filename.get(path.name.casefold(), ""),
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

    def start_proxy(self, video_id: str) -> dict[str, Any]:
        self.get_video(video_id)
        with self.lock:
            current = self.proxy_status(video_id)
            if current["status"] in {"ready", "running"}:
                return current
            self.proxy_jobs[video_id] = {"status": "running", "message": "正在生成兼容预览…"}
        self.proxy_executor.submit(self._make_proxy, video_id)
        return self.proxy_status(video_id)

    def _make_proxy(self, video_id: str) -> None:
        video = self.get_video(video_id)
        destination = self.proxy_path(video_id)
        temporary = destination.with_suffix(".tmp.mp4")
        result = run_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(video.path), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale='min(1280,iw)':-2:flags=lanczos,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-profile:v", "main", "-tag:v", "avc1",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            str(temporary),
        ])
        with self.lock:
            if result.returncode == 0 and temporary.exists():
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
            "fall": (self.output, "fall_export.csv", "跌倒"),
            "no_fall": (self.no_fall_output, "no_fall_label_export.csv", "不跌倒"),
            "caregiver_fall": (self.caregiver_output, "caregiver_fall_export.csv", "护工 Fall"),
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
            rows: dict[str, list[dict[str, Any]]] = {label: [] for label in destinations}
            failures: list[str] = []
            with self.lock:
                self.export_job.update(total=total, message=f"准备归档 {total} 个已标记原视频")
            for index, (video, label) in enumerate(plan, start=1):
                output, _csv_name, display = destinations[label]
                destination = output / video.name
                with self.lock:
                    self.export_job["message"] = f"正在移动 {index}/{total}（{display}）：{video.name}"
                try:
                    source_path = str(video.path)
                    self._move_original(video, destination)
                    rows[label].append({
                        "source": source_path, "relative_source": video.relative,
                        "duration": f"{video.duration:.6f}",
                        "label": label, "output": video.name,
                    })
                except Exception as exc:
                    failures.append(f"{video.name}：{exc}")
                with self.lock:
                    self.export_job["done"] = index

            self.scan()
            for label, category_rows in rows.items():
                if not category_rows:
                    continue
                try:
                    output, csv_name, display = destinations[label]
                    csv_path = output / csv_name
                    merged: dict[str, dict[str, Any]] = {}
                    if csv_path.exists():
                        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                            merged.update({row["output"]: row for row in csv.DictReader(handle) if row.get("output")})
                    merged.update({row["output"]: row for row in category_rows})
                    all_rows = list(merged.values())
                    temporary_csv = csv_path.with_suffix(".csv.tmp")
                    with temporary_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(category_rows[0]))
                        writer.writeheader()
                        writer.writerows(all_rows)
                    temporary_csv.replace(csv_path)
                except (OSError, csv.Error, KeyError, ValueError) as exc:
                    failures.append(f"{display}清单写入失败：{exc}")
            succeeded = sum(len(items) for items in rows.values())
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
                    "metadataCsv": str(self.server.app.metadata_csv) if self.server.app.metadata_csv else None,
                    "metadataMatched": sum(bool(video.metadata_device_id) for video in self.server.app.videos),
                    "metadataConflicts": len(self.server.app.metadata_conflicts),
                    "videos": self.server.app.public_videos(),
                })
            elif parsed.path == "/api/state":
                with self.server.app.lock:
                    self.send_json({"labels": self.server.app.public_labels()})
            elif parsed.path == "/api/export-status":
                with self.server.app.lock:
                    self.send_json(dict(self.server.app.export_job))
            elif parsed.path == "/api/proxy-status":
                self.send_json(self.server.app.proxy_status(query.get("id", [""])[0]))
            elif parsed.path.startswith("/media/"):
                self._send_file(self.server.app.get_video(parsed.path.removeprefix("/media/")).path, allow_range=True)
            elif parsed.path.startswith("/proxy/"):
                video_id = parsed.path.removeprefix("/proxy/")
                self.server.app.get_video(video_id)
                self._send_file(self.server.app.proxy_path(video_id), allow_range=True)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
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
                    "metadataCsv": str(self.server.app.metadata_csv) if self.server.app.metadata_csv else None,
                    "metadataMatched": sum(bool(video.metadata_device_id) for video in self.server.app.videos),
                    "metadataConflicts": len(self.server.app.metadata_conflicts),
                    "videos": self.server.app.public_videos(),
                })
            elif parsed.path == "/api/proxy":
                self.send_json(self.server.app.start_proxy(str(data.get("id", ""))))
            elif parsed.path == "/api/shutdown":
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
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
