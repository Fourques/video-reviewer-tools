"""Reusable rules only: never carry dataset paths, labels or project identity."""
import os
import threading
from pathlib import Path
from project_schema import preset, validate
from project_store import atomic_json, ProjectStore

_LOCK = threading.RLock()
FIELDS = {"labels", "tags", "intervals", "segmentSeconds", "freeSegments", "exportMode", "clipFlat", "wholeLabels", "negativeLabels", "shortcuts", "seekSeconds", "quickSubmit", "snapKeyframes", "deviceRegex"}

def portable_rules(config):
    if not isinstance(config, dict):
        raise ValueError("配置必须是对象")
    rules = {key: value for key, value in config.items() if key in FIELDS}
    checked = validate({**preset("custom"), **rules})
    return {key: value for key, value in checked.items() if key in FIELDS}

class RuleTemplates:
    def __init__(self, path=None):
        settings = Path(os.environ.get("VIDEO_REVIEWER_SETTINGS_FILE", str(Path.home() / ".video_reviewer_launcher.json")))
        self.path = Path(path) if path else settings.with_name(settings.stem + "_rules.json")
    def read(self):
        with _LOCK:
            return ProjectStore.read_json(self.path) if self.path.exists() else {"revision": 0, "templates": []}
    def save(self, payload):
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("配置名称须为 1–80 个字")
        rules = portable_rules(payload.get("config"))
        with _LOCK:
            data = self.read()
            if payload.get("revision") != data["revision"]:
                raise ValueError("配置列表已更新，请刷新后重新保存")
            items = [item for item in data["templates"] if item["name"] != name.strip()]
            if len(items) >= 100:
                raise ValueError("最多保存 100 套配置")
            items.append({"name": name.strip(), "config": rules})
            result = {"revision": data["revision"] + 1, "templates": items}
            atomic_json(self.path, result)
            return result
