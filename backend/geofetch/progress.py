"""Structured stage progress shared by providers and the local job supervisor."""
import json
import math
import time

PREFIX = '@@GEOFETCH_PROGRESS@@'
_last = None
_last_time = 0.0

def report(stage, completed=0, total=None, unit='', force=False):
    global _last, _last_time
    completed = max(0, completed)
    percent = min(100., max(0., completed * 100. / total)) if total is not None and total > 0 else (100. if total == 0 else None)
    value = dict(stage=stage, completed=completed, total=total, unit=unit, percent=round(percent, 1) if percent is not None else None)
    now = time.monotonic()
    changed_stage = _last is None or stage != _last['stage']
    terminal = percent == 100. and (_last is None or _last['percent'] != 100. or changed_stage)
    if not force and not changed_stage and not terminal and now - _last_time < .5:
        return
    _last, _last_time = value, now
    print('\n' + PREFIX + json.dumps(value, ensure_ascii=False), flush=True)
    if percent is not None:
        detail = f' ({completed:,}/{total:,} {unit})' if total is not None else ''
        print(f'[{stage}] {percent:.1f}%{detail}', flush=True)
    else:
        print(f'[{stage}] 处理中' + (f' ({completed:,} {unit})' if completed else ''), flush=True)

def track(items, total, stage='裁剪', unit='要素'):
    report(stage, 0, total, unit, force=True)
    for count, item in enumerate(items, 1):
        yield item
        report(stage, count, total, unit)

def parse(line):
    if not line.startswith(PREFIX):
        return None
    try:
        value = json.loads(line[len(PREFIX):])
        percent = value.get('percent')
        if not isinstance(value.get('stage'), str) or (percent is not None and (not isinstance(percent, (int, float)) or not math.isfinite(percent) or not 0 <= percent <= 100)):
            return None
        return value
    except (ValueError, TypeError):
        return None
