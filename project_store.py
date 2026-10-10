"""Portable, single-writer project persistence with a durable operation journal.

Only acknowledgement after journal fsync means 'saved'. Snapshots are caches:
an interrupted checkpoint is recovered by replaying the journal. OS locks release
on crash and protect independent app processes, including SMB mounts that support
file locking. Never silently reset unreadable progress.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import threading
import uuid
from pathlib import Path


class ConflictError(ValueError):
    pass


def atomic_json(path: Path, value) -> None:
    # Encode in memory, then perform a buffered sequential write. json.dump's
    # per-token writes/encoding are costly for large project snapshots on SMB.
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode('utf-8')
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ProjectStore:
    def __init__(self, directory: Path, progress=None):
        self.report = progress or (lambda *args: None)
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()
        self.handle = (directory / "writer.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write(b" ")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise ValueError("项目正在被其他程序编辑，或此网络盘不支持文件锁。请先退出另一个工具；不支持锁时可将整个项目复制到本机。") from exc
        self.closed = False
        self.path = directory / "state.json"
        self.journal = directory / "events.jsonl"
        self.warning = ""
        try:
            self.state = {"version": 2, "seq": 0, "assets": {}, "annotations": {}, "history": [], "operations": {}, "cursor": None, "views": [], "round": 1}
            if self.path.exists():
                self.state = self.read_json(self.path, self.report)
            if not isinstance(self.state, dict) or self.state.get("version") != 2:
                raise ValueError("进度格式不支持，请使用匹配版本；现有文件未修改")
            for key, kind in (("assets", dict), ("annotations", dict), ("history", list), ("operations", dict), ("views", list)):
                if not isinstance(self.state.get(key), kind):
                    raise ValueError(f"进度中的 {key} 已损坏；请保留项目目录进行恢复")
            self._replay()
        except Exception:
            self.closed = True
            self.handle.close()
            raise

    @staticmethod
    def read_json(path: Path, progress=None):
        try:
            if not progress:
                return json.loads(path.read_bytes())
            with path.open('rb') as handle:
                total = os.fstat(handle.fileno()).st_size
                progress('读取项目快照', 0, total, '正在读取已保存进度（字节）；不会读取视频内容')
                chunks, done = [], 0
                while chunk := handle.read(1024 * 1024):
                    chunks.append(chunk)
                    done += len(chunk)
                    progress('读取项目快照', done, total, '顺序读取已保存进度（字节）')
            progress('解析项目进度', 0, None, f'解析 {done / 1024 / 1024:.1f} MB 进度快照')
            return json.loads(b''.join(chunks))
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取项目数据：{path.name}。未清空进度，请检查网络盘或恢复此文件。") from exc

    def _replay(self):
        if not self.journal.exists():
            return
        valid_end = 0
        with self.journal.open("rb") as handle:
            total = os.fstat(handle.fileno()).st_size
            self.report('恢复操作日志', 0, total, '恢复已确认的标签和整理记录（字节）')
            for index, raw in enumerate(handle, 1):
                # Only a non-newline-terminated last write may be incomplete.
                if not raw.endswith(b"\n"):
                    self.warning = "上次退出留下未确认的半条记录，已恢复所有确认保存的标注。"
                    break
                try:
                    event = json.loads(raw)
                    sequence = int(event["seq"])
                    if sequence > self.state["seq"]:
                        if sequence != self.state["seq"] + 1:
                            raise ValueError("日志序号不连续")
                        self._apply(event)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError("项目操作日志损坏；未覆盖旧进度，请保留隐藏项目目录进行恢复。") from exc
                valid_end = handle.tell()
                if index % 100 == 0:
                    self.report('恢复操作日志', valid_end, total, f'已检查 {index} 条日志，原标签保留')
            self.report('恢复操作日志', valid_end, total, '已恢复确认保存的记录（字节）')
        if self.warning:
            with self.journal.open("r+b") as handle:
                handle.truncate(valid_end)
                handle.flush()
                os.fsync(handle.fileno())

    def _apply(self, event):
        for key, value in event["set"].items():
            self.state[key] = value
        for key, values in event.get("merge", {}).items():
            self.state[key].update(values)
        self.state["seq"] = event["seq"]

    def commit(self, changes=None, merge=None):
        with self.mutex:
            if self.closed:
                raise RuntimeError("项目已经关闭")
            event = {"seq": self.state["seq"] + 1, "set": changes or {}, "merge": merge or {}}
            payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            # Roll back an unsuccessful write, otherwise a retry could append after
            # an incomplete JSON line. No in-memory mutation before durable commit.
            with self.journal.open("a+b") as handle:
                offset = handle.tell()
                try:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError:
                    handle.truncate(offset)
                    raise
            self._apply(copy.deepcopy(event))
            if self.state["seq"] % 100 == 0:
                try:
                    self.checkpoint()
                except OSError:
                    self.warning = "标注已保存到操作日志，快照暂时写入失败；请检查剩余空间。"

    def checkpoint(self):
        with self.mutex:
            atomic_json(self.path, self.state)
            # Snapshot is durable before truncation; replay skips older sequences.
            with self.journal.open("wb") as handle:
                handle.flush()
                os.fsync(handle.fileno())

    def close(self):
        if getattr(self, "closed", True):
            return
        try:
            if hasattr(self, "state"):
                self.checkpoint()
        finally:
            self.closed = True
            self.handle.close()
