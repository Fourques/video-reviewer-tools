"""Resolve renamed review clips from bounded, read-only adjacent manifests."""
import csv
import json
import re
from pathlib import Path

UUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
EVENT = re.compile(UUID)
CAMERA = re.compile(r'kamicare_(' + UUID + r')_([^_/\\]+)_', re.I)

def basename(value):
    return str(value or '').replace('\\', '/').rsplit('/', 1)[-1]

def identity(name):
    match = CAMERA.search(basename(name))
    events = EVENT.findall(basename(name))
    return {'did': match[2] if match else '', 'community_id': match[1].lower() if match else '',
            'event_id': events[-1].lower() if events else '', 'original_name': basename(name)}

def read_source_identities(source, videos):
    """Inspect only known manifests in source + two parents, never video contents."""
    source = Path(source)
    candidates, warnings = {}, []
    names = {v.name for v in videos}
    events = {identity(name)['event_id'] for name in names} - {''}
    for directory in [source, *list(source.parents)[:2]]:
        for filename in ('summary.json', '视频清单.csv'):
            path = directory / filename
            try:
                if not path.is_file():
                    continue
                if path.stat().st_size > 20_000_000:
                    warnings.append(f'{filename} 超过20MB，未读取设备关联')
                    continue
                with path.open(encoding='utf-8-sig') as stream:
                    if filename.endswith('.json'):
                        payload = json.load(stream)
                        rows = payload.get('manifest', []) if isinstance(payload, dict) else payload
                    else:
                        rows = csv.DictReader(stream)
                    if not isinstance(rows, (list, csv.DictReader)):
                        continue
                    for index, row in enumerate(rows):
                        if index >= 50000:
                            warnings.append(f'{filename} 超过50000条，停止读取设备关联')
                            break
                        if not isinstance(row, dict):
                            continue
                        original = row.get('原视频路径') or row.get('original_video_path') or row.get('video_path') or ''
                        info = identity(original)
                        info['did'] = str(row.get('did') or row.get('device_id') or info['did'])
                        info['community_id'] = str(row.get('community_id') or info['community_id']).lower()
                        info['event_id'] = str(row.get('事件ID') or row.get('event_id') or info['event_id']).lower()
                        if not info['did']:
                            continue
                        relative = row.get('视频相对路径') or row.get('relative_path') or row.get('filename') or ''
                        key = basename(relative)
                        # A manifest's relative location must identify this directory,
                        # otherwise only an unambiguous event identity may be used.
                        location_matches = relative and (directory / str(relative).replace('\\', '/')).absolute() == (source / key).absolute()
                        keys = []
                        if location_matches and key in names:
                            keys.append(('name', key))
                        if info['event_id'] in events:
                            keys.append(('event', info['event_id']))
                        for candidate in keys:
                            candidates.setdefault(candidate, []).append({**info, 'manifest': filename})
            except (OSError, ValueError, csv.Error) as exc:
                warnings.append(f'{filename} 设备关联读取失败：{type(exc).__name__}')
    result = {}
    for name in names:
        direct = identity(name)
        options = candidates.get(('name', name)) or candidates.get(('event', direct['event_id'])) or []
        if direct['did']:
            options = [direct, *options]
        unique = {(r['did'], r['community_id']) for r in options}
        if len(unique) > 1:
            result[name] = {'conflict': True, 'event_id': direct['event_id']}
        elif options:
            result[name] = options[0]
    return result, warnings
