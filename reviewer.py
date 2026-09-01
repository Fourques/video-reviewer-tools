#!/usr/bin/env python3
"""Local browser-based reviewer for lossless 8-second video clipping.

Only Python's standard library is required. FFmpeg and FFprobe must be on PATH.
Final clips are always created with stream copy (-c copy); optional browser proxies
are review-only and are never used as an export source.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import shutil
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


CLIP_SECONDS = 8.0
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".mts", ".m2ts",
    ".ts", ".mpg", ".mpeg", ".flv", ".wmv",
}
APP_DIR = Path(__file__).resolve().parent
INDEX_FILE = APP_DIR / "index.html"


def run_command(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def ffprobe_duration(path: Path) -> float:
    result = run_command([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], timeout=60)
    if result.returncode != 0:
        return 0.0
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError:
        return 0.0


def ffprobe_keyframes(path: Path) -> list[float]:
    # Ask the decoder to emit only key frames, avoiding a huge packet listing for
    # hour-long files while retaining accurate presentation timestamps.
    result = run_command([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey", "-show_entries", "frame=best_effort_timestamp_time",
        "-of", "csv=p=0", str(path),
    ], timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "FFprobe 无法读取关键帧")
    keyframes: list[float] = []
    for line in result.stdout.splitlines():
        try:
            value = float(line.strip().split(",", 1)[0])
        except ValueError:
            continue
        if value >= 0:
            keyframes.append(round(value, 6))
    if not keyframes or keyframes[0] > 0.01:
        keyframes.insert(0, 0.0)
    return sorted(set(keyframes))


def nearest_keyframe_at_or_before(keyframes: list[float], requested: float) -> float:
    low, high = 0, len(keyframes)
    while low < high:
        middle = (low + high) // 2
        if keyframes[middle] <= requested + 1e-6:
            low = middle + 1
        else:
            high = middle
    return keyframes[max(0, low - 1)] if keyframes else 0.0


def safe_json_write(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def copy_file_bytes(source: Path, destination: Path) -> None:
    """Copy only file contents using basic reads/writes for network-drive compatibility."""
    with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle, length=4 * 1024 * 1024)


@dataclass(frozen=True)
class Video:
    id: str
    path: Path
    relative: str
    name: str
    size: int
    duration: float

    def public(
        self, selected: int, reviewed: bool, no_fall: bool, duplicate_name: bool,
    ) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "relative": self.relative,
            "size": self.size,
            "duration": self.duration,
            "selected": selected,
            "reviewed": reviewed,
            "noFall": no_fall,
            "duplicateName": duplicate_name,
        }


class ReviewApp:
    def __init__(self, source: Path, output: Path, no_fall_output: Path, cache: Path) -> None:
        self.source = source.resolve()
        self.output = output.resolve()
        self.no_fall_output = no_fall_output.resolve()
        self.cache = cache.resolve()
        if not self.source.is_dir():
            raise ValueError(f"源目录不存在：{self.source}")
        if self.source in {self.output, self.no_fall_output}:
            raise ValueError("输出目录不能与源目录相同")
        if self.output == self.no_fall_output:
            raise ValueError("片段输出目录和无跌倒输出目录不能相同")
        self.output.mkdir(parents=True, exist_ok=True)
        self.no_fall_output.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.state_file = self.output / ".clip_reviewer_state.json"
        self.lock = threading.RLock()
        self.keyframe_cache: dict[str, list[float]] = {}
        self.proxy_jobs: dict[str, dict[str, Any]] = {}
        self.export_job: dict[str, Any] = {"status": "idle", "message": "尚未导出"}
        self.state = self._load_state()
        self.videos: list[Video] = []
        self.video_by_id: dict[str, Video] = {}
        self.scan()


    def _load_state(self) -> dict[str, Any]:
        empty = {"source": str(self.source), "selections": {}, "reviewed": {}, "no_fall": {}}
        if not self.state_file.exists():
            return empty
        try:
            loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if loaded.get("source") != str(self.source):
            return empty
        loaded.setdefault("selections", {})
        loaded.setdefault("reviewed", {})
        loaded.setdefault("no_fall", {})
        return loaded

    def save_state(self) -> None:
        with self.lock:
            safe_json_write(self.state_file, self.state)

    def scan(self) -> None:
        paths: list[Path] = []
        for path in self.source.iterdir():
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            resolved = path.resolve()
            if (
                self.output in resolved.parents
                or self.no_fall_output in resolved.parents
                or self.cache in resolved.parents
            ):
                continue
            paths.append(path)
        paths.sort(key=lambda item: str(item.relative_to(self.source)).casefold())
        videos: list[Video] = []
        # A small pool keeps startup responsive on an SMB share without putting
        # excessive concurrent load on the NAS.
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(paths)))) as pool:
            durations = list(pool.map(ffprobe_duration, paths))
        for path, duration in zip(paths, durations):
            relative = path.relative_to(self.source).as_posix()
            identifier = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
            videos.append(Video(
                id=identifier,
                path=path.resolve(),
                relative=relative,
                name=path.name,
                size=path.stat().st_size,
                duration=duration,
            ))
        with self.lock:
            self.videos = videos
            self.video_by_id = {video.id: video for video in videos}

    def public_videos(self) -> list[dict[str, Any]]:
        names: dict[str, int] = {}
        for video in self.videos:
            names[video.name.casefold()] = names.get(video.name.casefold(), 0) + 1
        selections = self.state["selections"]
        reviewed = self.state["reviewed"]
        no_fall = self.state["no_fall"]
        return [
            video.public(
                len(selections.get(video.id, [])),
                bool(reviewed.get(video.id, False)),
                bool(no_fall.get(video.id, False)),
                names[video.name.casefold()] > 1,
            )
            for video in self.videos
        ]

    def get_video(self, video_id: str) -> Video:
        try:
            return self.video_by_id[video_id]
        except KeyError as exc:
            raise ValueError("视频不存在或列表已经变化") from exc

    def get_keyframes(self, video_id: str) -> list[float]:
        with self.lock:
            cached = self.keyframe_cache.get(video_id)
        if cached is not None:
            return cached
        video = self.get_video(video_id)
        frames = ffprobe_keyframes(video.path)
        with self.lock:
            self.keyframe_cache[video_id] = frames
        return frames

    def update_video_state(
        self, video_id: str, selections: list[dict[str, Any]], reviewed: bool, no_fall: bool,
    ) -> None:
        video = self.get_video(video_id)
        cleaned: list[dict[str, Any]] = []
        for entry in selections:
            start = round(float(entry.get("start", 0)), 6)
            label = str(entry.get("label", "未分类"))[:30]
            max_start = max(0.0, video.duration - CLIP_SECONDS)
            if start < 0 or start > max_start + 0.05:
                raise ValueError(f"片段起点超出范围：{start}")
            cleaned.append({"start": min(start, max_start), "label": label})
        cleaned.sort(key=lambda item: item["start"])
        for previous, current in zip(cleaned, cleaned[1:]):
            if current["start"] < previous["start"] + CLIP_SECONDS - 0.001:
                raise ValueError("同一视频中存在重叠片段，请调整后再保存")
        if no_fall and cleaned:
            raise ValueError("同一视频不能同时标记“全程无跌倒”和选择跌倒片段")
        with self.lock:
            self.state["selections"][video_id] = cleaned
            self.state["reviewed"][video_id] = bool(reviewed)
            self.state["no_fall"][video_id] = bool(no_fall)
            self.save_state()

    def proxy_path(self, video_id: str) -> Path:
        return self.cache / f"{video_id}.mp4"

    def proxy_status(self, video_id: str) -> dict[str, Any]:
        path = self.proxy_path(video_id)
        if path.exists() and path.stat().st_size > 0:
            return {"status": "ready", "url": f"/proxy/{quote(video_id)}"}
        with self.lock:
            return dict(self.proxy_jobs.get(video_id, {"status": "idle"}))

    def start_proxy(self, video_id: str) -> dict[str, Any]:
        self.get_video(video_id)
        current = self.proxy_status(video_id)
        if current["status"] in {"ready", "running"}:
            return current
        with self.lock:
            self.proxy_jobs[video_id] = {"status": "running", "message": "正在生成浏览器预览…"}
        threading.Thread(target=self._make_proxy, args=(video_id,), daemon=True).start()
        return self.proxy_status(video_id)

    def _make_proxy(self, video_id: str) -> None:
        video = self.get_video(video_id)
        destination = self.proxy_path(video_id)
        temporary = destination.with_suffix(".tmp.mp4")
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(video.path), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(temporary),
        ]
        result = run_command(command)
        with self.lock:
            if result.returncode == 0 and temporary.exists():
                temporary.replace(destination)
                self.proxy_jobs[video_id] = {
                    "status": "ready", "url": f"/proxy/{quote(video_id)}",
                    "message": "兼容预览已生成；最终导出仍使用原视频",
                }
            else:
                temporary.unlink(missing_ok=True)
                self.proxy_jobs[video_id] = {
                    "status": "error",
                    "message": result.stderr.strip()[-1000:] or "生成预览失败",
                }

    def start_export(self) -> dict[str, Any]:
        with self.lock:
            if self.export_job.get("status") == "running":
                return dict(self.export_job)
            self.export_job = {"status": "running", "done": 0, "total": 0, "message": "正在检查…"}
        threading.Thread(target=self._export_all, daemon=True).start()
        return dict(self.export_job)

    def _export_plan(self) -> tuple[list[dict[str, Any]], list[Video]]:
        clip_plan: list[dict[str, Any]] = []
        no_fall_plan: list[Video] = []
        for video in self.videos:
            entries = list(self.state["selections"].get(video.id, []))
            entries.sort(key=lambda entry: float(entry["start"]))
            no_fall = bool(self.state["no_fall"].get(video.id, False))
            if entries and no_fall:
                raise ValueError(f"{video.relative} 同时存在已选片段和“全程无跌倒”标记")
            if no_fall:
                no_fall_plan.append(video)
            stem, suffix = video.path.stem, video.path.suffix
            for index, entry in enumerate(entries, start=1):
                filename = video.name if len(entries) == 1 else f"{stem}_{index:04d}{suffix}"
                clip_plan.append({"video": video, "entry": entry, "filename": filename})
        if not clip_plan and not no_fall_plan:
            raise ValueError("还没有选择片段，也没有标记全程无跌倒的视频")

        destinations: dict[str, list[str]] = {}
        for item in clip_plan:
            destinations.setdefault(item["filename"].casefold(), []).append(item["video"].relative)
        duplicates = {name: paths for name, paths in destinations.items() if len(paths) > 1}
        if duplicates:
            detail = "; ".join(f"{name}: {', '.join(paths)}" for name, paths in duplicates.items())
            raise ValueError(f"扁平输出目录存在文件名冲突，请先处理同名源视频：{detail}")

        no_fall_names: dict[str, list[str]] = {}
        for video in no_fall_plan:
            no_fall_names.setdefault(video.name.casefold(), []).append(video.relative)
        no_fall_duplicates = {name: paths for name, paths in no_fall_names.items() if len(paths) > 1}
        if no_fall_duplicates:
            detail = "; ".join(f"{name}: {', '.join(paths)}" for name, paths in no_fall_duplicates.items())
            raise ValueError(f"无跌倒输出目录存在文件名冲突，请先处理同名源视频：{detail}")

        existing = [
            str(self.output / item["filename"])
            for item in clip_plan
            if (self.output / item["filename"]).exists()
        ]
        existing.extend(
            str(self.no_fall_output / video.name)
            for video in no_fall_plan
            if (self.no_fall_output / video.name).exists()
        )
        if existing:
            raise ValueError("为避免覆盖，以下输出文件已存在：" + "; ".join(existing))
        return clip_plan, no_fall_plan

    def _export_all(self) -> None:
        try:
            clip_plan, no_fall_plan = self._export_plan()
            total = len(clip_plan) + len(no_fall_plan)
            with self.lock:
                self.export_job.update(
                    total=total,
                    message=f"准备导出 {len(clip_plan)} 段，并复制 {len(no_fall_plan)} 个无跌倒原视频",
                )
            rows: list[dict[str, Any]] = []
            done = 0
            for item in clip_plan:
                video: Video = item["video"]
                entry = item["entry"]
                requested = float(entry["start"])
                frames = self.get_keyframes(video.id)
                actual = nearest_keyframe_at_or_before(frames, requested)
                destination = self.output / item["filename"]
                with self.lock:
                    self.export_job["message"] = f"正在导出 {done + 1}/{total}：{item['filename']}"
                output_duration = self._stream_copy_clip(video.path, actual, destination)
                rows.append({
                    "source": str(video.path),
                    "relative_source": video.relative,
                    "requested_start": f"{requested:.6f}",
                    "actual_keyframe_start": f"{actual:.6f}",
                    "target_duration": f"{CLIP_SECONDS:.3f}",
                    "actual_output_duration": f"{output_duration:.6f}",
                    "label": entry.get("label", "未分类"),
                    "output": item["filename"],
                })
                done += 1
                with self.lock:
                    self.export_job["done"] = done

            if rows:
                csv_path = self.output / "clips.csv"
                temporary_csv = csv_path.with_suffix(".csv.tmp")
                with temporary_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                temporary_csv.replace(csv_path)

            no_fall_rows: list[dict[str, Any]] = []
            no_fall_failures: list[str] = []
            for video in no_fall_plan:
                destination = self.no_fall_output / video.name
                temporary = self.no_fall_output / f".{video.name}.clip-reviewer-{os.getpid()}-{time.time_ns()}.tmp"
                with self.lock:
                    self.export_job["message"] = f"正在复制 {done + 1}/{total}：{video.name}"
                try:
                    try:
                        copy_file_bytes(video.path, temporary)
                    except OSError as exc:
                        raise RuntimeError(f"复制无跌倒原视频失败：{video.name}：{exc}") from exc
                    try:
                        temporary.replace(destination)
                    except OSError as exc:
                        raise RuntimeError(f"完成复制但保存文件名失败：{video.name}：{exc}") from exc
                    no_fall_rows.append({
                        "source": str(video.path),
                        "relative_source": video.relative,
                        "duration": f"{video.duration:.6f}",
                        "output": video.name,
                    })
                except Exception as exc:
                    no_fall_failures.append(str(exc))
                finally:
                    temporary.unlink(missing_ok=True)
                    done += 1
                    with self.lock:
                        self.export_job["done"] = done

            if no_fall_rows:
                csv_path = self.no_fall_output / "no_fall.csv"
                temporary_csv = csv_path.with_suffix(".csv.tmp")
                with temporary_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(no_fall_rows[0]))
                    writer.writeheader()
                    writer.writerows(no_fall_rows)
                temporary_csv.replace(csv_path)
            with self.lock:
                if no_fall_failures:
                    self.export_job.update(
                        status="error", done=total, total=total,
                        message=(
                            f"部分完成：{len(clip_plan)} 段跌倒片段已导出；"
                            f"无跌倒视频成功 {len(no_fall_rows)} 个、失败 {len(no_fall_failures)} 个。\n"
                            + "\n".join(no_fall_failures[:10])
                        ),
                    )
                    return
                self.export_job.update(
                    status="done", done=total, total=total,
                    message=(
                        f"完成：已无损导出 {len(clip_plan)} 段到 {self.output}；"
                        f"已原样复制 {len(no_fall_plan)} 个无跌倒视频到 {self.no_fall_output}"
                    ),
                )
        except Exception as exc:  # The message is surfaced in the local UI.
            with self.lock:
                self.export_job.update(status="error", message=str(exc))

    def _stream_copy_clip(self, source: Path, start: float, destination: Path) -> float:
        """Remux without re-encoding and choose the packet cut closest to 8 seconds.

        FFmpeg's output duration is packet-quantized and can exceed ``-t`` because
        of B-frame reordering or audio priming. A few cheap remux attempts let us
        compensate for that without ever decoding/re-encoding either stream.
        """
        attempts: list[tuple[float, Path]] = []
        requested_duration = CLIP_SECONDS
        token = f"{os.getpid()}-{time.time_ns()}"
        last_error = ""
        try:
            for attempt in range(4):
                temporary = destination.with_name(
                    f".{destination.stem}.clip-reviewer-{token}-{attempt}{destination.suffix}"
                )
                command = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                    "-ss", f"{start:.6f}", "-i", str(source),
                    "-t", f"{requested_duration:.6f}",
                    "-map", "0", "-c", "copy", "-map_metadata", "0",
                    "-avoid_negative_ts", "make_zero", str(temporary),
                ]
                result = run_command(command)
                if result.returncode != 0:
                    last_error = result.stderr.strip()[-1200:]
                    temporary.unlink(missing_ok=True)
                    break
                observed = ffprobe_duration(temporary)
                if observed <= 0:
                    temporary.unlink(missing_ok=True)
                    last_error = "FFprobe 无法读取临时成品时长"
                    break
                attempts.append((observed, temporary))
                error = CLIP_SECONDS - observed
                if abs(error) <= 0.025:
                    break
                adjusted = requested_duration + error
                requested_duration = min(CLIP_SECONDS + 1.0, max(CLIP_SECONDS - 1.0, adjusted))

            if not attempts:
                raise RuntimeError(f"导出 {destination.name} 失败：{last_error or 'FFmpeg 未生成文件'}")
            best_duration, best_path = min(attempts, key=lambda item: abs(item[0] - CLIP_SECONDS))
            best_path.replace(destination)
            return best_duration
        finally:
            for _, path in attempts:
                if path != destination:
                    path.unlink(missing_ok=True)


class ReviewHandler(BaseHTTPRequestHandler):
    server: "ReviewServer"

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
                    "source": str(self.server.app.source),
                    "output": str(self.server.app.output),
                    "noFallOutput": str(self.server.app.no_fall_output),
                    "clipSeconds": CLIP_SECONDS,
                    "videos": self.server.app.public_videos(),
                })
            elif parsed.path == "/api/state":
                with self.server.app.lock:
                    self.send_json({
                        "source": str(self.server.app.source),
                        "selections": self.server.app.state["selections"],
                        "reviewed": self.server.app.state["reviewed"],
                        "noFall": self.server.app.state["no_fall"],
                    })
            elif parsed.path == "/api/keyframes":
                video_id = query.get("id", [""])[0]
                self.send_json({"keyframes": self.server.app.get_keyframes(video_id)})
            elif parsed.path == "/api/export-status":
                with self.server.app.lock:
                    self.send_json(dict(self.server.app.export_job))
            elif parsed.path == "/api/proxy-status":
                video_id = query.get("id", [""])[0]
                self.send_json(self.server.app.proxy_status(video_id))
            elif parsed.path.startswith("/media/"):
                video = self.server.app.get_video(parsed.path.removeprefix("/media/"))
                self._send_file(video.path, allow_range=True)
            elif parsed.path.startswith("/proxy/"):
                video_id = parsed.path.removeprefix("/proxy/")
                self.server.app.get_video(video_id)
                path = self.server.app.proxy_path(video_id)
                if not path.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self._send_file(path, allow_range=True)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            data = self.read_json()
            if parsed.path == "/api/video-state":
                self.server.app.update_video_state(
                    str(data.get("id", "")),
                    list(data.get("selections", [])),
                    bool(data.get("reviewed", False)),
                    bool(data.get("noFall", False)),
                )
                self.send_json({"ok": True})
            elif parsed.path == "/api/proxy":
                self.send_json(self.server.app.start_proxy(str(data.get("id", ""))))
            elif parsed.path == "/api/export":
                self.send_json(self.server.app.start_export())
            elif parsed.path == "/api/rescan":
                self.server.app.scan()
                self.send_json({"ok": True, "videos": self.server.app.public_videos()})
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
                video = self.server.app.get_video(parsed.path.removeprefix("/media/"))
                self._send_file(video.path, allow_range=True, send_body=False)
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
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range") if allow_range else None
        if range_header and range_header.startswith("bytes="):
            requested = range_header[6:].split(",", 1)[0]
            first, _, last = requested.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else end
                elif last:
                    suffix_length = int(last)
                    start = max(0, size - suffix_length)
            except ValueError:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            if start < 0 or start >= size or end < start:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

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


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: ReviewApp) -> None:
        super().__init__(address, ReviewHandler)
        self.app = app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="人工审核并无损截取 8 秒视频片段")
    parser.add_argument("--source", required=True, help="待审核视频目录（只扫描当前目录第一层）")
    parser.add_argument("--output", help="8 秒片段的扁平输出目录；默认是源目录/output")
    parser.add_argument("--no-fall-output", help="全程无跌倒原视频输出目录；默认是源目录/no_fall_output")
    parser.add_argument("--cache", default=str(APP_DIR / ".preview_cache"), help="浏览器兼容预览缓存目录")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只允许本机访问")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("错误：PATH 中找不到 ffmpeg 或 ffprobe", file=sys.stderr)
        return 2
    source = Path(args.source).expanduser()
    if not source.is_dir():
        print(f"错误：源目录不存在：{source}", file=sys.stderr)
        return 2
    output = Path(args.output).expanduser() if args.output else source / "output"
    no_fall_output = (
        Path(args.no_fall_output).expanduser()
        if args.no_fall_output
        else source / "no_fall_output"
    )
    if source.resolve() == output.resolve() or source.resolve() == no_fall_output.resolve():
        print("错误：输出目录不能与源目录相同，以免覆盖原视频", file=sys.stderr)
        return 2
    if output.resolve() == no_fall_output.resolve():
        print("错误：片段输出目录和无跌倒输出目录不能相同", file=sys.stderr)
        return 2
    print(f"正在扫描视频目录：{source.resolve()}", flush=True)
    app = ReviewApp(source, output, no_fall_output, Path(args.cache).expanduser())
    server = ReviewServer((args.host, args.port), app)
    url = f"http://{args.host}:{args.port}"
    print(f"已找到 {len(app.videos)} 个视频")
    print(f"源目录：{app.source}")
    print(f"8 秒片段输出目录：{app.output}")
    print(f"无跌倒原视频输出目录：{app.no_fall_output}")
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
