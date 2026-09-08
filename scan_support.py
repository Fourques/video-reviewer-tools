"""Reusable duration cache for the clip workflow; no video data is changed."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed


def cached_durations(paths, cache_file, probe, progress):
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    result = [0.0] * len(paths)
    keys = []
    missing = []
    done = 0
    for index, path in enumerate(paths):
        stat = path.stat()
        key = hashlib.sha256(f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()
        keys.append(key)
        duration = cache.get(key)
        if isinstance(duration, (int, float)) and duration > 0:
            result[index] = duration
            done += 1
        else:
            missing.append(index)
    progress("读取视频时长", done, len(paths), f"复用 {done} 个缓存；其余视频首次读取")
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(probe, paths[index]): index for index in missing}
        for task in as_completed(pending):
            index = pending[task]
            try:
                result[index] = task.result()
            except Exception:
                result[index] = 0.0
            done += 1
            progress("读取视频时长", done, len(paths), paths[index].name)
    try:
        temporary = cache_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({key: value for key, value in zip(keys, result) if value > 0}), encoding="utf-8")
        temporary.replace(cache_file)
    except OSError:
        pass  # An unavailable cache must not prevent reviewing.
    return result
