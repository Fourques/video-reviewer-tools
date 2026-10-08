"""Small explicit project schema. Display names never serve as label IDs."""
from __future__ import annotations

import copy
import math
import re
import uuid

VERSION = "2.0.5"
KEYS = {"play": "s", "loop": "w", "back": "a", "forward": "d", "stepBack": "arrowleft", "stepForward": "arrowright", "previous": "[", "next": "]", "addSegment": "space", "complete": "enter", "undo": "backspace", "review": "u"}
KEYS.update({action: '' for action in ('settings', 'organize', 'search', 'rescan', 'proxy', 'proxyAll', 'fullscreen', 'mute', 'setStart', 'setEnd', 'previewSegment', 'saveView', 'switchProject', 'clearLabel', 'previousDevice', 'nextDevice')})


def preset(name="fall"):
    labels = [
        {"id": "fall", "name": "跌倒", "description": "确认真实跌倒", "color": "#df6b75", "key": "j", "folder": "fall", "active": True},
        {"id": "no_fall", "name": "不跌倒", "description": "确认没有跌倒", "color": "#49b99e", "key": "k", "folder": "no_fall", "active": True},
        {"id": "caregiver_fall", "name": "护工 Fall", "description": "护工相关跌倒", "color": "#ad8cdd", "key": "l", "folder": "caregiver_fall", "active": True},
    ]
    if name == "clips":
        labels.extend([
            {"id": "scene_change", "name": "场景变化", "description": "明显场景切换", "color": "#e4b56a", "key": "", "folder": "scene_change", "active": True},
            {"id": "motion", "name": "大幅运动", "description": "明显画面运动", "color": "#65a8df", "key": "", "folder": "motion", "active": True},
            {"id": "unclassified", "name": "未分类", "description": "已选择但尚未确定类型", "color": "#8e98a7", "key": "", "folder": "unclassified", "active": True},
        ])
    if name == "custom":
        labels = [{"id": "label_1", "name": "类别 1", "description": "", "color": "#78a9ef", "key": "j", "folder": "label_1", "active": True}]
    return {"version": 2, "id": str(uuid.uuid4()), "name": "", "preset": name, "labels": labels, "tags": [], "intervals": name == "clips", "segmentSeconds": 8, "freeSegments": False, "exportMode": "copy" if name == "clips" else ("labels" if name == "custom" else "move"), "output": "output", "clipOutput": "output/clips", "clipFlat": True, "wholeLabels": ["no_fall"] if name == "clips" else [], "negativeLabels": ["no_fall"] if name != "custom" else [], "destinations": {}, "inputs": [], "shortcuts": copy.deepcopy(KEYS), "seekSeconds": 1, "quickSubmit": name != "clips", "snapKeyframes": True, "metadataConfig": {}}


def validate(config, previous=None, used=None):
    result = copy.deepcopy(config)
    result["version"] = 2
    result["name"] = str(result.get("name", ""))[:150].strip()
    if result.get("exportMode") not in {"labels", "copy", "move"}:
        raise ValueError("请选择仅保存标注、复制或移动")
    for field in ("segmentSeconds", "seekSeconds"):
        value = float(result.get(field, 8 if field == "segmentSeconds" else 1))
        if not math.isfinite(value) or not 0.04 <= value <= 86400:
            raise ValueError("时间必须是 0.04–86400 之间的有限数字")
        result[field] = value
    for field in ("output", "clipOutput"):
        if not str(result.get(field, "")).strip():
            raise ValueError("请填写输出目录")
    labels = result.get("labels")
    if not isinstance(labels, list) or not labels:
        raise ValueError("至少保留一个标签")
    shortcuts = {**KEYS, **result.get("shortcuts", {})}
    allowed = set(KEYS)
    if set(shortcuts) != allowed:
        raise ValueError("快捷键包含不支持的操作")
    reserved = {"tab", "escape", "shift", "control", "alt", "meta", "capslock", "unidentified", "dead"}
    seen_keys = {}
    for action, raw in shortcuts.items():
        key = str(raw).strip().lower()
        if key and (key in reserved or (len(key) > 1 and key not in {"space", "enter", "backspace", "arrowleft", "arrowright", "arrowup", "arrowdown", "delete", "home", "end"} and not re.fullmatch(r"f[1-9][0-2]?", key))):
            raise ValueError(f"不支持的快捷键：{key}")
        if key and key in seen_keys:
            raise ValueError(f"快捷键 {key} 重复使用")
        if key:
            seen_keys[key] = action
        shortcuts[action] = key
    ids, folders = set(), set()
    for label in labels:
        identity = str(label.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identity) or identity in ids:
            raise ValueError("标签 ID 无效或重复；已使用标签请停用，不要改变 ID")
        ids.add(identity)
        label["name"] = str(label.get("name", "")).strip()[:100]
        if not label["name"]:
            raise ValueError("标签名称不能为空")
        label["description"] = str(label.get("description", ""))[:1000]
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(label.get("color", ""))):
            raise ValueError("标签颜色需要六位十六进制格式")
        folder = str(label.get("folder", identity)).strip()
        if folder in {".", ".."} or not folder or re.search(r'[<>:"/\\|?*\x00-\x1f]', folder) or folder.endswith((".", " ")) or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", folder):
            raise ValueError(f"输出子目录不能跨目录或使用系统保留名：{folder}")
        if folder.casefold() in folders:
            raise ValueError("标签输出子目录不能重复")
        folders.add(folder.casefold())
        label["folder"] = folder
        label["active"] = bool(label.get("active", True))
        key = str(label.get("key", "")).lower().strip()
        if key and (len(key) != 1 or (label['active'] and key in seen_keys)):
            raise ValueError(f"标签快捷键必须是未占用的单个键：{key}")
        if key and label["active"]:
            seen_keys[key] = identity
        label["key"] = key
    if not any(label["active"] for label in labels):
        raise ValueError("至少启用一个标签")
    if previous and not set(used or ()).issubset(ids):
        raise ValueError("已有标注引用的标签不能删除，请取消启用")
    tags = result.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 100 for tag in tags) or len(tags) != len(set(tags)):
        raise ValueError("辅助标签不能为空或重复")
    result["shortcuts"] = shortcuts
    for field in ("wholeLabels", "negativeLabels"):
        if not set(result.get(field, [])).issubset(ids):
            raise ValueError(f"{field} 中有不存在的标签")
    if not isinstance(result.get("inputs", []), list):
        raise ValueError("额外目录必须是列表")
    for field in ("intervals", "freeSegments", "clipFlat", "quickSubmit", "snapKeyframes"):
        result[field] = bool(result.get(field, False))
    if result.get('deviceRegex'):
        try:
            pattern = re.compile(result['deviceRegex'])
            if not pattern.groups:
                raise ValueError('设备正则需包含一个捕获括号')
        except re.error as exc:
            raise ValueError('设备正则表达式无效') from exc
    result.setdefault('destinations', {})
    if not isinstance(result['destinations'], dict) or not set(result['destinations']).issubset(ids):
        raise ValueError('独立目录绑定中含不存在的标签')
    return result
