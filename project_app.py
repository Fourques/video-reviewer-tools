"""Unified whole-video and temporal annotation service.

The v1 adapters remain only for migration and reusable media/CSV utilities.
This is the sole project service used by start.py in v2.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import heapq
import json
import math
import os
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from quick_labeler import LabelApp, LabelHandler, Video, sha256_file
from reviewer import VIDEO_EXTENSIONS, copy_file_bytes, ffprobe_duration, ffprobe_keyframes, nearest_keyframe_at_or_before, run_command, user_cache_dir
from project_schema import VERSION, preset, validate
from project_store import ConflictError, ProjectStore, atomic_json

APP_DIR = Path(__file__).resolve().parent


class ProjectApp(LabelApp):
    def __init__(self, source: Path, selection=None, progress=None):
        self.source = source.expanduser().resolve()
        if not self.source.is_dir():
            raise ValueError("项目目录不存在")
        self.report = progress or (lambda *args: None)
        self.selection = selection or {}
        self.webm_preview = self.selection.get('preview_format') == 'webm'
        self.store = ProjectStore(self.source / ".video-reviewer")
        self.lock = self.store.mutex
        self.state = self.store.state
        self.config_path = self.store.directory / "project.json"
        self.cache = user_cache_dir("unified-preview")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.closing = threading.Event()
        self.video_by_id, self.videos = {}, []
        self.proxy_jobs, self.info_cache = {}, {}
        self.proxy_all_job = {"status": "idle", "done": 0, "total": 0}
        self.export_job = {"status": "idle", "done": 0, "total": 0, "failures": []}
        self.metadata_csv = None
        self.metadata_config, self.metadata_rows = {}, {}
        self.metadata_columns, self.detected_csvs = [], []
        self.metadata_conflicts = set()
        self.proxy_condition = threading.Condition(self.lock)
        self.proxy_queue, self.proxy_counter = [], 0
        self.proxy_threads = []
        self.migration = self.state.get("migration", {"imported": 0, "files": []})
        try:
            if self.config_path.exists():
                self.config = validate(ProjectStore.read_json(self.config_path))
            else:
                name = self.selection.get("preset") or ("clips" if self.selection.get("mode") == "clip" else "fall")
                self.config = preset(name)
                self.config["name"] = self.source.name
                if self.selection.get("projectConfig"):
                    imported = self.selection["projectConfig"]
                    self.config.update({key: value for key, value in imported.items() if key not in {"id", "version", "metadataConfig", "destinations", "inputs"}})
                if self.selection.get("output_root"):
                    self.config["output"] = self.location(Path(self.selection["output_root"]))
                    self.config["clipOutput"] = self.location(Path(self.selection["output_root"]) / "clips")
                # Honor explicit v1 CLI/output bindings, and discover existing
                # category directories without changing their locations.
                for key, label, default in (("fall_output", "fall", "fall_output"), ("no_fall_output", "no_fall", "no_fall_output"), ("caregiver_fall_output", "caregiver_fall", "caregiver_fall_output")):
                    raw = self.selection.get(key)
                    path = Path(raw) if raw else self.source / default
                    if raw or path.is_dir():
                        self.config["destinations"][label] = self.location(path)
                if self.selection.get("output") and name == "clips":
                    self.config["clipOutput"] = self.location(Path(self.selection["output"]))
                self.config = validate(self.config)
                atomic_json(self.config_path, self.config)
            saved_meta = self.config.get("metadataConfig", {})
            if saved_meta.get("path"):
                saved_meta = {**saved_meta, "path": str(self.resolve(saved_meta["path"]))}
            if saved_meta:
                self.state["metadataConfig"] = saved_meta
            self._recover_operations()
            self.scan()
            if not self.state.get("migrationComplete"):
                self._migrate()
            for index in range(2):
                worker = threading.Thread(target=self._proxy_worker, daemon=True, name=f"preview-{index}")
                self.proxy_threads.append(worker)
                worker.start()
        except Exception:
            self.closing.set()
            self.store.close()
            raise

    def location(self, path: Path):
        path = path.expanduser().resolve()
        try:
            return path.relative_to(self.source).as_posix()
        except ValueError:
            return str(path)

    def resolve(self, value):
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.source / path).resolve()

    def destination(self, label):
        custom = self.config.get("destinations", {}).get(label)
        item = next(item for item in self.config["labels"] if item["id"] == label)
        return self.resolve(custom) if custom else self.resolve(self.config["output"]) / item["folder"]

    def save_state(self):
        # CSV utility's only state mutation is its selected mapping.
        self.store.commit({"metadataConfig": copy.deepcopy(self.state.get("metadataConfig", {}))})

    def _discover_csvs(self):
        found = []
        # Stay near the batch, never enumerate eight ancestors of a NAS mount.
        for depth, directory in enumerate([self.source, *list(self.source.parents)[:3]]):
            if directory == Path(directory.anchor) or directory in {Path('/tmp'), Path.home()}:
                break
            try:
                candidates = [path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == '.csv' and not path.name.startswith('.')]
            except OSError:
                continue
            candidates.sort(key=lambda path: (path.name.lower() not in {'index.csv', 'candidates.csv', 'metadata.csv'}, path.name.lower()))
            found.extend(candidates[:12 if depth == 0 else 6])
        return found

    def _auto_metadata_config(self, video_keys):
        result = super()._auto_metadata_config(video_keys)
        if not result:
            return {}
        # The v1 heuristic accepted an unrelated CSV with zero matches.
        path, column = Path(result['path']), result['fileColumn']
        with path.open(encoding='utf-8-sig', newline='') as handle:
            for index, row in enumerate(csv.DictReader(handle)):
                if any(key in video_keys for key in self._metadata_keys(str(row.get(column, '')))):
                    return result
                if index >= 100_000:
                    break
        return {}

    def _load_metadata(self, video_keys):
        self.metadata_warning = ''
        saved = self.state.get('metadataConfig', {})
        if saved.get('path') and not Path(saved['path']).is_file():
            self.metadata_csv, self.metadata_rows, self.metadata_columns = None, {}, []
            self.metadata_config = dict(saved)
            self.metadata_conflicts = set()
            self.detected_csvs = self._discover_csvs()
            self.metadata_warning = '已配置 CSV 不可访问；原映射保留，请在设置中重新绑定，未自动换用其他 CSV'
            return
        super()._load_metadata(video_keys)
        if saved.get('path') and self.metadata_config:
            # Explicitly selecting zero reference fields or no device field is
            # a legitimate choice, not a request to guess different columns.
            for field in ('deviceColumn', 'labelColumns'):
                if field in saved:
                    self.metadata_config[field] = copy.deepcopy(saved[field])
        if saved.get('path') and not self.metadata_config:
            self.metadata_config = dict(saved)
            self.metadata_warning = '已配置 CSV 无法读取，原映射保留，请检查编码或表头'

    def scan(self):
        with self.lock:
            self._scan()

    def _scan(self):
        self.input_warnings = []
        roots = [(self.source, None)]
        roots.extend((self.resolve(value), None) for value in self.config.get("inputs", []))
        for label in self.config["labels"]:
            if not self.config["wholeLabels"] or label["id"] in self.config["wholeLabels"]:
                roots.append((self.destination(label["id"]), label["id"]))
        unique = {}
        for path, label in roots:
            # Category assignment wins if an input is itself a category folder.
            if path not in unique or label:
                unique[path] = label
        found = []
        copied = {self.resolve(record.get('destinationLocation', record['destination'])) for record in self.state['operations'].values() if record.get('kind') == 'copy' and record.get('status') == 'done' and record.get('id') in self.state['assets'] and self.resolve(self.state['assets'][record['id']]['path']).is_file()}
        for directory, label in unique.items():
            if not directory.is_dir():
                if label is None:
                    self.input_warnings.append(f'输入目录不可访问：{directory}；该目录进度保留，可在设置中重新绑定')
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    if Path(entry.name).suffix.lower() in VIDEO_EXTENSIONS and entry.is_file(follow_symlinks=False):
                        stat = entry.stat()
                        if (directory / entry.name).resolve() in copied:
                            continue
                        found.append((directory / entry.name, label, stat.st_size, stat.st_mtime_ns))
                        if len(found) % 100 == 0:
                            self.report("检索当前目录", len(found), None, directory.name)
        # Existing project members retain their canonical location when output
        # bindings change. Do not recursively discover anything in those folders.
        seen_paths = {self.location(path) for path, *_ in found}
        for asset in self.state['assets'].values():
            if asset['path'] in seen_paths:
                continue
            path = self.resolve(asset['path'])
            if path.is_file():
                stat = path.stat()
                found.append((path, asset.get('originLabel'), stat.st_size, stat.st_mtime_ns))
                seen_paths.add(asset['path'])
        found.sort(key=lambda item: (item[0].name.casefold(), str(item[0].parent)))
        video_keys = {key for path, *_ in found for key in (path.name.casefold(), path.stem.casefold())}
        self._load_metadata(video_keys)
        assets = copy.deepcopy(self.state["assets"])
        by_path = {value["path"]: key for key, value in assets.items()}
        orphan_by_name = {}
        for key, value in assets.items():
            if not self.resolve(value["path"]).exists():
                orphan_by_name.setdefault((value["name"], value["size"]), []).append(key)
        videos = []
        for path, origin, size, modified in found:
            location = self.location(path)
            identity = by_path.get(location)
            candidates = orphan_by_name.get((path.name, size), [])
            if not identity and len(candidates) == 1:
                identity = candidates.pop()  # unique relocated asset, not a copy
            identity = identity or str(uuid.uuid4())
            previous = assets.get(identity, {})
            # Replacing a file at the same path must not inherit old annotations.
            if previous and (previous["size"] != size or previous.get("modified", modified) != modified):
                identity = str(uuid.uuid4())
                previous = {}
            assets[identity] = {**previous, "path": location, "name": path.name, "size": size, "modified": modified, "originLabel": origin}
            row = self.metadata_rows.get(path.name.casefold()) or self.metadata_rows.get(path.stem.casefold())
            column = self.metadata_config.get("deviceColumn", "")
            videos.append(Video(identity, path, location, path.name, size, self.info_cache.get(identity, {}).get("duration", 0), origin, str(row.get(column, "")) if row and column else "", row is not None, tuple((column, str(row.get(column, "")) if row else "") for column in self.metadata_config.get("labelColumns", []))))
        self.store.commit({"assets": assets})
        self.videos, self.video_by_id = videos, {video.id: video for video in videos}
        self._save_metadata_config()

    def _save_metadata_config(self):
        config = copy.deepcopy(self.metadata_config)
        if config.get("path"):
            config["path"] = self.location(Path(config["path"]))
        if self.config.get("metadataConfig") != config:
            candidate = {**self.config, "metadataConfig": config, 'revision': self.config.get('revision', 0) + 1}
            atomic_json(self.config_path, candidate)
            self.config = candidate
            self.info_cache.clear()

    def _migrate(self):
        candidates = set()
        for directory in {self.source / "output", self.resolve(self.config["clipOutput"]), *(self.destination(item["id"]) for item in self.config["labels"])}:
            if directory.is_dir():
                candidates.update(directory.glob(".fall_label_state*.json"))
                candidates.update(directory.glob(".clip_reviewer_state.json"))
        annotations = copy.deepcopy(self.state["annotations"])
        imported, files, warnings = 0, [], []
        name_counts = {}
        for video in self.videos:
            name_counts[video.name] = name_counts.get(video.name, 0) + 1
        for path in sorted(candidates):
            legacy = ProjectStore.read_json(path)
            if not isinstance(legacy, dict):
                raise ValueError(f"旧进度结构损坏：{path}；未删除旧文件")
            old_source = str(legacy.get("source", ""))
            if old_source != str(self.source) and not path.is_relative_to(self.source):
                continue  # external shared output belongs to another project
            files.append(self.location(path))
            for video in self.videos:
                if name_counts[video.name] == 1:
                    legacy_ids = [hashlib.sha256(video.name.encode()).hexdigest()[:20]]
                    legacy_ids.extend(hashlib.sha256(f"category:{label}:{video.name}".encode()).hexdigest()[:20] for label in ('fall', 'no_fall', 'caregiver_fall'))
                else:
                    old_identity = f'category:{video.origin_label}:{video.name}' if video.origin_label else video.name
                    legacy_ids = [hashlib.sha256(old_identity.encode()).hexdigest()[:20]]
                    warnings.append(f'同名视频仅按原目录身份迁移，不猜测跨目录进度：{video.relative}')
                value = annotations.get(video.id, self.empty_annotation())
                changed = False
                for old_id in legacy_ids:
                    label = legacy.get("labels", {}).get(old_id)
                    if label in {item["id"] for item in self.config["labels"]}:
                        value.update(label=label, status="done", imported=True)
                        changed = True
                    selections = legacy.get("selections", {}).get(old_id)
                    if selections is not None:
                        mapping = {"跌倒": "fall", "场景变化": "scene_change", "大幅运动": "motion", "未分类": "unclassified"}
                        segments = []
                        for entry in selections:
                            mapped = mapping.get(entry.get("label"), "unclassified")
                            if mapped not in {item["id"] for item in self.config["labels"]}:
                                self.config["labels"].append(next(item for item in preset("clips")["labels"] if item["id"] == mapped))
                            segments.append({"id": str(uuid.uuid4()), "start": float(entry["start"]), "end": float(entry["start"]) + 8, "label": mapped})
                        value["segments"] = segments
                        if segments:
                            self.config["intervals"] = True
                        changed = True
                    if legacy.get('no_fall', {}).get(old_id):
                        value['label'] = 'no_fall'
                        changed = True
                    if legacy.get('reviewed', {}).get(old_id):
                        value['status'] = 'done' if value['label'] or value['segments'] else 'review'
                        changed = True
                if changed:
                    annotations[video.id] = value
                    imported += 1
        self.migration = {"imported": imported, "files": files, "warnings": warnings, "at": time.time()}
        atomic_json(self.config_path, validate(self.config))
        self.store.commit({"annotations": annotations, "migration": self.migration, "migrationComplete": True})
        self.store.checkpoint()

    def empty_annotation(self):
        return {"label": None, "tags": [], "segments": [], "status": "pending", "note": "", "revision": 0, "round": self.state.get("round", 1)}

    def annotation(self, identity):
        value = copy.deepcopy(self.state["annotations"].get(identity, self.empty_annotation()))
        if value.get("round", 1) != self.state.get("round", 1):
            value["status"] = "pending"
        return value

    def public_videos(self):
        counts = {}
        for video in self.videos:
            counts[video.name.casefold()] = counts.get(video.name.casefold(), 0) + 1
        return [{**video.public(self.annotation(video.id)["label"], counts[video.name.casefold()] > 1), "annotation": self.annotation(video.id)} for video in self.videos]

    def document(self):
        with self.lock:
            missing = [value["name"] for key, value in self.state["assets"].items() if key not in self.video_by_id]
            return {"version": VERSION, "source": str(self.source), "config": self.config, "videos": self.public_videos(), "cursor": self.state.get("cursor"), "views": self.state["views"], "seq": self.state["seq"], "round": self.state["round"], "migration": self.migration, "warning": self.store.warning, "metadataWarning": self.metadata_warning, 'inputWarnings': self.input_warnings, "missing": missing, **self.metadata_info()}

    def save_annotation(self, payload):
        with self.lock:
            identity = str(payload.get("id", ""))
            self.get_video(identity)
            current = self.annotation(identity)
            token = str(payload.get("token", ""))[:100]
            if token and current.get("token") == token:
                return current
            if int(payload.get("revision", -1)) != current["revision"]:
                raise ConflictError("这条视频已被其他页面修改，请重新载入后再提交")
            value = {**self.empty_annotation(), **copy.deepcopy(payload["annotation"])}
            all_labels = {item["id"] for item in self.config["labels"]}
            if value["label"] is not None and value["label"] not in all_labels:
                raise ValueError("标签不属于当前项目")
            if value["status"] not in {"pending", "done", "review"}:
                raise ValueError("审核状态无效")
            if not set(value["tags"]).issubset(self.config["tags"]):
                raise ValueError("辅助标签不属于当前项目")
            value["note"] = str(value["note"])[:2000]
            if not isinstance(value["segments"], list):
                raise ValueError("片段必须是列表")
            ids = set()
            for segment in value["segments"]:
                start, end = float(segment["start"]), float(segment["end"])
                if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
                    raise ValueError("片段起止时间无效")
                if segment["label"] not in all_labels:
                    raise ValueError("片段标签不属于项目")
                segment["start"], segment["end"] = round(start, 6), round(end, 6)
                segment["id"] = str(segment.get("id") or uuid.uuid4())
                if segment["id"] in ids:
                    raise ValueError("片段 ID 重复")
                ids.add(segment["id"])
            if value["segments"]:
                if not self.config["intervals"]:
                    raise ValueError("请先在项目设置中开启区间标注")
                duration = self.video_info(identity)["duration"]
                if duration <= 0 or any(segment["end"] > duration + 0.08 for segment in value["segments"]):
                    raise ValueError("片段超过原视频时长")
                value["segments"].sort(key=lambda segment: segment["start"])
            if value["label"] in self.config["negativeLabels"] and value["segments"]:
                raise ValueError("负类标签不能同时包含已选择片段，请确认后修改")
            if value["status"] == "done" and not value["label"] and not value["segments"]:
                raise ValueError("请选择标签或区间；没有选片段不代表无跌倒，可标记待复核")
            value.update(revision=current["revision"] + 1, round=self.state["round"], token=token, updatedAt=time.time())
            history = (self.state["history"] + [{"id": identity, "before": current, "afterRevision": value["revision"]}])[-100:]
            self.store.commit({"history": history}, {"annotations": {identity: value}})
            return value

    def undo(self):
        with self.lock:
            if not self.state["history"]:
                raise ValueError("没有可撤销的标注")
            history = list(self.state["history"])
            item = history.pop()
            current = self.annotation(item["id"])
            if current["revision"] != item["afterRevision"]:
                raise ConflictError("这条记录已被修改，不能覆盖较新的标注")
            value = {**item["before"], "revision": current["revision"] + 1, "token": "", "updatedAt": time.time()}
            # Earlier history entries on the same video must recognize the undo
            # revision, so multiple undo steps remain possible.
            for previous in reversed(history):
                if previous["id"] == item["id"]:
                    previous["afterRevision"] = value["revision"]
                    break
            self.store.commit({"history": history}, {"annotations": {item["id"]: value}})
            return {"id": item["id"], "annotation": value}

    def configure(self, payload):
        with self.lock:
            if self.export_job["status"] == "running":
                raise ValueError("整理运行中，请完成后再修改项目规则")
            if int(payload.get("seq", -1)) != self.state["seq"]:
                raise ConflictError("项目已有新数据，请重新打开设置后修改")
            if payload.get('configRevision', self.config.get('revision', 0)) != self.config.get('revision', 0):
                raise ConflictError('项目规则已被其他页面修改，请重新打开设置；未覆盖较新规则')
            used = {label for annotation in self.state["annotations"].values() for label in [annotation.get("label"), *(segment["label"] for segment in annotation.get("segments", []))] if label}
            candidate = validate({**self.config, **payload["config"], "id": self.config["id"]}, self.config, used)
            candidate['revision'] = self.config.get('revision', 0) + 1
            if not candidate["intervals"] and any(annotation.get("segments") for annotation in self.state["annotations"].values()):
                raise ValueError("已有区间标注，不能关闭区间功能")
            for location in candidate.get("inputs", []):
                if not self.resolve(location).is_dir():
                    raise ValueError(f"额外输入目录不可访问：{location}")
            removed_tags = set(self.config["tags"]) - set(candidate["tags"])
            if any(removed_tags.intersection(annotation.get("tags", [])) for annotation in self.state["annotations"].values()):
                raise ValueError("已使用的辅助标签不能删除")
            atomic_json(self.config_path, candidate)
            self.config = candidate
            self.scan()
            return self.document()

    def video_info(self, identity):
        video = self.get_video(identity)
        stat = video.path.stat()
        fingerprint = (stat.st_size, stat.st_mtime_ns)
        cached = self.info_cache.get(identity)
        if cached and cached.get("fingerprint") == fingerprint:
            return cached
        duration = ffprobe_duration(video.path)
        value = {"duration": duration, "fingerprint": fingerprint, "keyframes": []}
        if self.config["intervals"] and self.config["snapKeyframes"]:
            value["keyframes"] = ffprobe_keyframes(video.path)
        self.info_cache[identity] = value
        return value

    def start_proxy(self, identity, priority=0):
        claimed = self._claim_proxy(identity)
        if claimed == "claimed" or (claimed == "running" and self.proxy_jobs.get(identity, {}).get("queued")):
            with self.proxy_condition:
                self.proxy_counter += 1
                self.proxy_jobs[identity].update(queued=True, ticket=self.proxy_counter)
                heapq.heappush(self.proxy_queue, (priority, self.proxy_counter, identity))
                self.proxy_condition.notify()
        return self.proxy_status(identity)

    def proxy_path(self, identity):
        path = super().proxy_path(identity)
        return path.with_suffix('.webm') if self.webm_preview else path

    def _make_proxy(self, identity):
        if not self.webm_preview:
            return super()._make_proxy(identity)
        temporary = None
        try:
            video = self.get_video(identity)
            destination = self.proxy_path(identity)
            temporary = destination.with_name(f'.{destination.stem}.{uuid.uuid4().hex}.tmp.webm')
            def command(audio):
                return ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-fflags', '+genpts', '-i', str(video.path), '-map', '0:v:0', *(['-map', '0:a:0?', '-c:a', 'libopus'] if audio else ['-an']), '-vf', "scale='min(1280,iw)':-2:flags=bicubic,format=yuv420p", '-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '8', '-row-mt', '1', '-threads', '2', '-crf', '28', '-b:v', '0', '-sn', '-dn', str(temporary)]
            result = run_command(command(True))
            if result.returncode:
                result = run_command(command(False))
            if result.returncode or not temporary.is_file() or not temporary.stat().st_size:
                raise RuntimeError(result.stderr.strip()[-1000:] or '生成预览失败')
            temporary.replace(destination)  # only the tool-owned preview cache
            with self.lock:
                self.proxy_jobs[identity] = {'status': 'ready', 'url': f'/proxy/{identity}', 'message': '已生成 VP9 兼容预览；原视频未修改'}
        except Exception as exc:
            with self.lock:
                self.proxy_jobs[identity] = {'status': 'error', 'message': str(exc)}
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _proxy_worker(self):
        while not self.closing.is_set():
            with self.proxy_condition:
                self.proxy_condition.wait_for(lambda: self.proxy_queue or self.closing.is_set())
                if self.closing.is_set():
                    return
                _, ticket, identity = heapq.heappop(self.proxy_queue)
                if self.proxy_jobs.get(identity, {}).get("ticket") != ticket:
                    continue
                self.proxy_jobs[identity]["queued"] = False
            self._make_proxy(identity)

    def _make_all_proxies(self):
        ids = [video.id for video in self.videos]
        ready = failed = 0
        for index, identity in enumerate(ids, 1):
            if self.closing.is_set():
                break
            self.start_proxy(identity, priority=10)
            while self.proxy_status(identity).get("status") == "running" and not self.closing.wait(0.1):
                pass
            if self.proxy_status(identity).get("status") == "ready":
                ready += 1
            else:
                failed += 1
            with self.lock:
                self.proxy_all_job.update(done=index, ready=ready, failed=failed, total=len(ids))
        with self.lock:
            self.proxy_all_job.update(status="done" if not failed else "error", message=f"兼容预览：成功 {ready}，失败 {failed}；原视频未修改")

    def export_plan(self):
        with self.lock:
            if self.config["exportMode"] == "labels":
                return {"items": [], "counts": {}, "conflicts": [], "message": "此项目仅保存标注；需要视频文件时请在项目设置选择复制或移动"}
            items, counts, conflicts, destinations = [], {}, [], {}
            for video in self.videos:
                annotation = self.annotation(video.id)
                if annotation["status"] != "done":
                    continue
                segments = annotation["segments"]
                for index, segment in enumerate(segments, 1):
                    name = video.name if len(segments) == 1 else f"{video.path.stem}_{index:04d}{video.path.suffix}"
                    directory = self.resolve(self.config["clipOutput"])
                    if not self.config["clipFlat"]:
                        directory /= next(item["folder"] for item in self.config["labels"] if item["id"] == segment["label"])
                    items.append({"id": video.id, "kind": "clip", "label": segment["label"], "segment": segment, "destination": str(directory / name)})
                label = annotation["label"]
                if label and not segments and (not self.config["wholeLabels"] or label in self.config["wholeLabels"]):
                    destination = self.destination(label) / video.name
                    if video.path.resolve() != destination.resolve():
                        items.append({"id": video.id, "kind": self.config["exportMode"], "label": label, "destination": str(destination)})
            pending = []
            for item in items:
                video = self.get_video(item["id"])
                item["source"] = str(video.path)
                item["size"] = video.size
                item['destinationLocation'] = self.location(Path(item['destination']))
                item['sourceLocation'] = self.location(video.path)
                signature = json.dumps([item["id"], item["kind"], item["destinationLocation"], item.get("segment"), video.size, video.path.stat().st_mtime_ns], sort_keys=True)
                item["key"] = hashlib.sha256(signature.encode()).hexdigest()
                key = str(Path(item["destination"]).resolve()).casefold()
                if key in destinations:
                    conflicts.append(f"多个成品同名：{Path(item['destination']).name}")
                destinations[key] = item["key"]
                receipt = self.state["operations"].get(item["key"])
                if receipt and receipt.get("status") == "done" and Path(item["destination"]).is_file():
                    stat = Path(item['destination']).stat()
                    unchanged = stat.st_size == receipt.get('outputSize') and stat.st_mtime_ns == receipt.get('outputModified')
                    if unchanged or (receipt.get('digest') and sha256_file(Path(item['destination'])) == receipt['digest']):
                        continue
                    conflicts.append(f"已整理成品被修改，未覆盖：{Path(item['destination']).name}")
                if Path(item["destination"]).exists() and item["kind"] == "clip":
                    conflicts.append(f"已有片段，未覆盖：{Path(item['destination']).name}")
                pending.append(item)
                counts[item["label"]] = counts.get(item["label"], 0) + 1
            return {"items": pending, "counts": counts, "conflicts": conflicts, "message": f"准备处理 {len(pending)} 项；未审核和待复核不整理"}

    def start_export(self, sequence=None):
        with self.lock:
            if self.export_job["status"] == "running":
                return dict(self.export_job)
            if sequence is not None and sequence != self.state["seq"]:
                raise ConflictError("确认期间标注发生变化，请重新预览整理计划")
            plan = self.export_plan()
            if plan["conflicts"]:
                raise ValueError("\n".join(plan["conflicts"][:20]))
            self.export_job = {"status": "running", "done": 0, "total": len(plan["items"]), "failures": [], "message": plan["message"]}
            threading.Thread(target=self._export, args=(plan["items"],), daemon=True).start()
            return dict(self.export_job)

    def _clip(self, video, item, temporary):
        segment = item["segment"]
        keyframes = self.video_info(video.id)['keyframes'] or ffprobe_keyframes(video.path)
        start = nearest_keyframe_at_or_before(keyframes, segment["start"])
        duration = segment["end"] - segment["start"]
        result = run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-ss", f"{start:.6f}", "-i", str(video.path), "-t", f"{duration:.6f}", "-map", "0", "-c", "copy", "-map_metadata", "0", "-avoid_negative_ts", "make_zero", str(temporary)])
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-1200:] or "截取失败")
        return {"actualStart": start, "requestedStart": segment["start"], "requestedEnd": segment["end"], "actualDuration": ffprobe_duration(temporary)}

    def _record_operation(self, key, value):
        with self.lock:
            self.store.commit(merge={"operations": {key: value}})

    @staticmethod
    def _publish_original(temporary, destination, digest):
        """Publish without replacing a concurrently-created destination.

        NAS mounts may not implement hard links. Exclusive creation is the
        fallback; a partial file created by this call is removed on failure.
        """
        try:
            os.link(temporary, destination)
            return
        except FileExistsError:
            raise ValueError("目标文件已存在，未覆盖")
        except OSError:
            pass
        created = False
        try:
            with destination.open('xb') as output, temporary.open('rb') as source:
                created = True
                import shutil
                shutil.copyfileobj(source, output, 4 * 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if sha256_file(destination) != digest:
                raise ValueError("发布内容校验失败，原文件保留")
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise

    def _move_original(self, video, destination):
        digest = sha256_file(video.path)
        if destination.exists():
            if not destination.is_file() or sha256_file(destination) != digest:
                raise ValueError("同名文件内容不同，未覆盖")
        else:
            temporary = destination.with_name(f'.{destination.name}.{uuid.uuid4().hex}.tmp')
            try:
                copy_file_bytes(video.path, temporary)
                if sha256_file(temporary) != digest:
                    raise ValueError("移动内容校验失败，原文件保留")
                self._publish_original(temporary, destination, digest)
            finally:
                temporary.unlink(missing_ok=True)
        # Verify again before the only destructive step, including concurrent
        # changes to the source during a long network copy.
        if sha256_file(video.path) != digest or sha256_file(destination) != digest:
            raise ValueError("移动期间文件发生变化，原文件保留")
        video.path.unlink()

    def _export(self, items):
        failures = []
        for index, item in enumerate(items, 1):
            temporary = None
            try:
                video = self.get_video(item["id"])
                stat = video.path.stat()
                asset = self.state['assets'][video.id]
                if stat.st_size != asset['size'] or stat.st_mtime_ns != asset['modified']:
                    raise ValueError("原视频已在审核后发生变化，请重新扫描并审核；未整理")
                destination = Path(item["destination"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                with self.lock:
                    self.export_job["message"] = f"{index}/{len(items)} · {video.name}"
                record = {**item, "status": "running", "startedAt": time.time()}
                if item["kind"] == "clip":
                    if destination.exists():
                        raise ValueError("输出片段已存在，未覆盖")
                    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp{destination.suffix}")
                    record["temporary"] = str(temporary)
                    self._record_operation(item["key"], record)
                    record.update(self._clip(video, item, temporary))
                    if record["actualDuration"] <= 0:
                        raise ValueError("输出片段无法读取，未发布成品")
                    record["digest"] = sha256_file(temporary)
                    self._record_operation(item["key"], record)
                    # User/external file could appear while FFmpeg was running.
                    if destination.exists():
                        raise ValueError("输出文件在导出期间出现，未覆盖")
                    self._publish_original(temporary, destination, record['digest'])
                else:
                    record["digest"] = sha256_file(video.path)
                    self._record_operation(item["key"], record)
                    if item["kind"] == "move":
                        self._move_original(video, destination)
                    else:
                        if destination.exists():
                            if sha256_file(destination) != record["digest"]:
                                raise ValueError("同名文件内容不同，未覆盖")
                        else:
                            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
                            copy_file_bytes(video.path, temporary)
                            if sha256_file(temporary) != record["digest"]:
                                raise ValueError("复制内容校验失败")
                            if destination.exists():
                                raise ValueError("目标文件在复制期间出现，未覆盖")
                            self._publish_original(temporary, destination, record['digest'])
                    if item["kind"] == "move":
                        with self.lock:
                            asset = {**self.state["assets"][video.id], "path": self.location(destination), "originLabel": item["label"], "modified": destination.stat().st_mtime_ns}
                            self.store.commit(merge={"assets": {video.id: asset}})
                            updated = replace(video, path=destination, relative=asset["path"], origin_label=item["label"])
                            self.video_by_id[video.id] = updated
                            self.videos = [updated if value.id == video.id else value for value in self.videos]
                output_stat = destination.stat()
                record.update(status="done", finishedAt=time.time(), outputSize=output_stat.st_size, outputModified=output_stat.st_mtime_ns)
                self._record_operation(item["key"], record)
            except Exception as exc:
                failures.append({"id": item["id"], "name": Path(item["source"]).name, "error": str(exc)})
                try:
                    self._record_operation(item["key"], {**item, "status": "error", "error": str(exc)})
                except OSError:
                    pass
            finally:
                if temporary:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass  # A cleanup failure must not stop later items.
                with self.lock:
                    self.export_job.update(done=index, failures=list(failures))
        with self.lock:
            self.export_job.update(status="error" if failures else "done", message=f"成功 {len(items)-len(failures)} 项，失败 {len(failures)} 项；失败项可重试，原 CSV 未修改")

    def _recover_operations(self):
        for key, record in list(self.state["operations"].items()):
            if record.get("status") != "running":
                continue
            destination = self.resolve(record.get('destinationLocation', record['destination']))
            # Rebind receipts inside a relocated project to its new root.
            asset = self.state["assets"].get(record["id"])
            if destination.is_file() and record.get("digest") and sha256_file(destination) == record["digest"]:
                if record["kind"] == "move" and asset:
                    source = self.resolve(record.get('sourceLocation', record['source']))
                    if source.exists() and source.resolve() != destination.resolve():
                        if sha256_file(source) != record['digest']:
                            self._record_operation(key, {**record, 'status': 'error', 'error': '中断后原文件发生变化，请人工确认；未删除'})
                            continue
                        source.unlink()
                    self.store.commit(merge={"assets": {record["id"]: {**asset, "path": self.location(destination), "originLabel": record["label"], "modified": destination.stat().st_mtime_ns}}})
                output_stat = destination.stat()
                self._record_operation(key, {**record, "status": "done", "recovered": True, 'outputSize': output_stat.st_size, 'outputModified': output_stat.st_mtime_ns})
            else:
                self._record_operation(key, {**record, "status": "error", "error": "上次整理中断，原数据保留，请重新整理"})

    def close(self):
        self.closing.set()
        with self.proxy_condition:
            self.proxy_condition.notify_all()
        self.store.close()


class ProjectHandler(LabelHandler):
    def do_GET(self):
        if self.runtime_get():
            return
        route = urlparse(self.path)
        query = parse_qs(route.query)
        try:
            app = self.server.app
            if route.path == "/":
                self._send_file(APP_DIR / "workbench.html", False)
            elif route.path in {"/assets/workbench.js", "/assets/workbench.css", "/assets/media.js"}:
                self._send_file(APP_DIR / route.path.rsplit("/", 1)[-1], False)
            elif route.path == "/api/project":
                self.send_json(app.document())
            elif route.path == "/api/video-info":
                self.send_json(app.video_info(query.get("id", [""])[0]))
            elif route.path == "/api/export-plan":
                self.send_json({**app.export_plan(), "seq": app.state["seq"]})
            elif route.path == "/api/export-status":
                self.send_json(dict(app.export_job))
            elif route.path == "/api/proxy-all-status":
                self.send_json(dict(app.proxy_all_job))
            elif route.path == "/api/proxy-status":
                self.send_json(app.proxy_status(query.get("id", [""])[0]))
            elif route.path == "/api/csv-columns":
                self.send_json(app.csv_columns(query.get("path", [""])[0]))
            elif route.path == "/api/csv-browser":
                self.send_json(app.csv_browser(query.get("path", [str(app.source)])[0]))
            elif route.path == "/api/directories":
                self.send_json(self.server.launcher_app.directories(query.get("path", [str(app.source)])[0]))
            elif route.path == "/api/config":
                self.send_json(self.server.launcher_app.config())
            elif route.path in {"/launcher", "/projects"}:
                self._send_file(APP_DIR / "project_center.html", False)
            elif route.path.startswith(("/media/", "/proxy/")):
                identity = unquote(route.path.rsplit("/", 1)[-1])
                video = app.get_video(identity)
                self._send_file(video.path if route.path.startswith("/media/") else app.proxy_path(identity), True)
            elif route.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self.send_json({"error": "Not Found"}, 404)
        except (ValueError, RuntimeError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_HEAD(self):
        route = urlparse(self.path).path
        try:
            if route == "/":
                self._send_file(APP_DIR / "workbench.html", False, False)
            elif route.startswith(("/media/", "/proxy/")):
                identity = unquote(route.rsplit("/", 1)[-1])
                video = self.server.app.get_video(identity)
                self._send_file(video.path if route.startswith("/media/") else self.server.app.proxy_path(identity), True, False)
            else:
                self.send_error(404)
        except (ValueError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self):
        if self.runtime_post():
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.send_json({"error": "不允许跨站修改项目"}, 403)
            return
        if "application/json" not in self.headers.get("Content-Type", ""):
            self.send_json({"error": "需要 JSON 请求"}, 415)
            return
        try:
            data = self.read_json()
            if not isinstance(data, dict):
                raise ValueError('请求必须是 JSON 对象')
            app = self.server.app
            route = urlparse(self.path).path
            if route == "/api/annotation":
                self.send_json({"annotation": app.save_annotation(data), "seq": app.state["seq"]})
            elif route == "/api/undo":
                self.send_json({**app.undo(), "seq": app.state["seq"]})
            elif route == "/api/project-config":
                self.send_json(app.configure(data))
            elif route == "/api/rescan":
                app.scan()
                self.send_json(app.document())
            elif route == "/api/cursor":
                identity = data.get("id")
                if identity:
                    app.get_video(identity)
                with app.lock:
                    app.store.commit({"cursor": identity})
                self.send_json({"seq": app.state["seq"]})
            elif route == "/api/views":
                views = data.get("views", [])
                if not isinstance(views, list) or len(views) > 30:
                    raise ValueError("最多保存 30 个视图")
                with app.lock:
                    app.store.commit({"views": views})
                self.send_json({"seq": app.state["seq"]})
            elif route == "/api/new-round":
                with app.lock:
                    if app.export_job["status"] == "running":
                        raise ValueError("请等待整理任务完成")
                    app.store.commit({"round": app.state["round"] + 1, "history": [], "cursor": None})
                self.send_json(app.document())
            elif route == "/api/proxy":
                self.send_json(app.start_proxy(str(data.get("id", "")), priority=5 if data.get("warm") else 0))
            elif route == "/api/proxy-all":
                self.send_json(app.start_proxy_all())
            elif route == "/api/metadata-config":
                app.configure_metadata(data)
                self.send_json(app.document())
            elif route == "/api/export":
                self.send_json(app.start_export(data.get("seq")))
            elif route == "/api/project-switch":
                if app.export_job["status"] == "running":
                    raise ValueError("整理中不能切换项目")
                app.close()
                self.server.app = self.server.launcher_app
                from launcher_server import LauncherHandler
                self.server.RequestHandlerClass = LauncherHandler
                with self.server.runtime.lock:
                    self.server.runtime.progress.update(status="idle")
                self.send_json({"ok": True})
            elif route in {"/api/shutdown", "/api/quit"}:
                if app.export_job['status'] == 'running':
                    raise ValueError('请等待整理完成再退出，避免中断文件移动')
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_json({"error": "Not Found"}, 404)
        except ConflictError as exc:
            self.send_json({"error": str(exc), "conflict": True}, 409)
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)
