"""Unified local application and CLI entry point."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'backend'))
os.environ.setdefault('PYTHONUTF8', '1')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def main():
    parser = argparse.ArgumentParser(description='basic geoinfo fetch · 基础地理信息数据获取')
    sub = parser.add_subparsers(dest='command')
    serve = sub.add_parser('serve', help='启动本地工作台（默认）')
    serve.add_argument('--no-browser', action='store_true')
    collect = sub.add_parser('collect', help='从命令行执行采集')
    collect.add_argument('--datasets', nargs='+', required=True)
    collect.add_argument('--area', default='')
    collect.add_argument('--output', default='data')
    collect.add_argument('--landcover-source', default='')
    collect.add_argument('--euluc-source', default='')
    collect.add_argument('--euluc-member', default='按城市划分.zip/按城市划分/重庆市.shp')
    collect.add_argument('--event-limit', type=int, default=3)
    collect.add_argument('--event-codes', nargs='*', default=[])
    sub.add_parser('catalog', help='列出支持的数据类型')
    args = parser.parse_args()
    if args.command == 'catalog':
        from geofetch.settings import CATALOG
        for item in CATALOG:
            print(f'{item["id"]:14} {item["name"]} / {item["format"]}')
        return 0
    if args.command == 'collect':
        from geofetch.api import TaskRequest, create_job
        from geofetch.jobs import manager
        from geofetch.settings import RUNTIME
        from fastapi import HTTPException
        request = vars(args).copy()
        request.pop('command')
        try:
            job = create_job(TaskRequest(**request))
        except HTTPException as exc:
            print(f'无法开始：{exc.detail}', file=sys.stderr)
            return 1
        offset = 0
        try:
            while True:
                log = RUNTIME / job['id'] / 'log.txt'
                if log.exists():
                    with log.open('rb') as stream:
                        stream.seek(offset)
                        chunk = stream.read()
                        offset = stream.tell()
                        print(chunk.decode('utf-8', errors='replace'), end='', flush=True)
                with manager.lock:
                    status = manager.jobs[job['id']]['status']
                if status not in {'queued', 'running', 'cancelling'}:
                    print(f'\n状态：{status}\n保存位置：{job["output"]}')
                    return 0 if status == 'completed' else 1
                time.sleep(.5)
        except KeyboardInterrupt:
            manager.cancel(job['id'])
            return 130
    import uvicorn
    if not getattr(args, 'no_browser', False):
        timer = threading.Timer(1.2, lambda: webbrowser.open('http://127.0.0.1:8765'))
        timer.daemon = True
        timer.start()
    uvicorn.run('geofetch.api:app', host='127.0.0.1', port=8765, reload=False)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
