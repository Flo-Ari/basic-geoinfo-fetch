import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .settings import ROOT, SOURCES, CACHE, DATA, RUNTIME, CATALOG, local_path
from .jobs import manager

@asynccontextmanager
async def lifespan(app):
    yield
    with manager.lock:
        for key in list(manager.jobs):
            manager.cancel(key)
    manager.executor.shutdown(wait=False, cancel_futures=True)

app = FastAPI(title='basic geoinfo fetch', version='1.0.0', lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'testserver'])

@app.middleware('http')
async def local_origin(request, call_next):
    origin = request.headers.get('origin')
    if origin and origin not in {'http://127.0.0.1:8765', 'http://localhost:8765', 'http://127.0.0.1:5173', 'http://localhost:5173'}:
        return JSONResponse({'detail': '仅允许本机界面调用'}, status_code=403)
    if request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.headers.get('sec-fetch-site') == 'cross-site':
        return JSONResponse({'detail': '不允许跨站请求'}, status_code=403)
    return await call_next(request)

class TaskRequest(BaseModel):
    datasets: list[str] = Field(min_length=1, max_length=8)
    area: str = ''
    output: str = 'data'
    landcover_source: str = ''
    euluc_source: str = ''
    euluc_member: str = '按城市划分.zip/按城市划分/重庆市.shp'
    event_limit: int = Field(default=3, ge=1, le=100)
    event_codes: list[str] = Field(default_factory=list, max_length=70)

def get_job(key):
    with manager.lock:
        if key not in manager.jobs:
            raise HTTPException(404, '任务不存在')
        return dict(manager.jobs[key])

def validate_area(path):
    try:
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('范围文件不能超过 10 MB')
        obj = json.loads(path.read_text(encoding='utf-8-sig'))
        crs = str(obj.get('crs', '')).upper()
        if crs and '4326' not in crs and 'CRS84' not in crs:
            raise ValueError('请先将范围文件转换为 WGS84 / EPSG:4326')
        from shapely.geometry import shape
        features = obj.get('features', []) if obj.get('type') == 'FeatureCollection' else [obj]
        if not features:
            raise ValueError('范围文件没有要素')
        for feature in features:
            geom = shape(feature.get('geometry') if feature.get('type') == 'Feature' else feature)
            if geom.is_empty or not geom.is_valid:
                raise ValueError('范围包含空或无效几何，请在 GIS 中修复')
            x1, y1, x2, y2 = geom.bounds
            if not (-180 <= x1 <= x2 <= 180 and -90 <= y1 <= y2 <= 90):
                raise ValueError('范围坐标超出经纬度范围，请使用 EPSG:4326')
        return len(features)
    except Exception as exc:
        raise HTTPException(400, f'范围文件无法使用：{exc}') from exc

@app.get('/api/config')
def config():
    boundaries = [{'name': p.relative_to(SOURCES).as_posix(), 'path': str(p)} for folder in ('boundaries','examples') for p in (SOURCES / folder).rglob('*.geojson')]
    return dict(catalog=CATALOG, paths=dict(cache=str(CACHE), sources=str(SOURCES), output=str(DATA)), boundaries=boundaries)

@app.post('/api/area')
async def upload_area(file: UploadFile = File(...)):
    if not (file.filename or '').lower().endswith('.geojson'):
        raise HTTPException(400, '请选择 .geojson 范围文件')
    content = await file.read(10 * 1024 * 1024 + 1)
    await file.close()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, '范围文件不能超过 10 MB')
    name = re.sub(r'[^\w.\-]', '_', Path(file.filename).name)
    path = SOURCES / 'boundaries' / f'{uuid.uuid4().hex[:8]}_{name}'
    path.write_bytes(content)
    try:
        count = validate_area(path)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    return dict(path=str(path), name=name, features=count)

@app.post('/api/pick-directory')
def pick_directory():
    # Native dialog runs in a separate process, not in the ASGI thread's Tcl context.
    code = 'import tkinter as t; from tkinter import filedialog; r=t.Tk(); r.withdraw(); r.attributes("-topmost", True); print(filedialog.askdirectory(title="选择文件夹")); r.destroy()'
    try:
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, encoding='utf-8',
                                env={**os.environ, 'PYTHONUTF8':'1'}, timeout=180,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError('当前环境不支持文件夹选择器，请直接填写完整路径')
        return dict(path=result.stdout.strip())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(400, str(exc))

@app.post('/api/jobs', status_code=201)
def create_job(body: TaskRequest):
    request = body.model_dump()
    request['datasets'] = list(dict.fromkeys(body.datasets))
    catalog = {item['id']: item for item in CATALOG}
    if any(key not in catalog for key in body.datasets):
        raise HTTPException(400, '包含不支持的数据类型')
    if any(catalog[key]['area'] for key in body.datasets):
        area = local_path(body.area) if body.area else None
        if not area or not area.is_file() or area.suffix.lower() != '.geojson':
            raise HTTPException(400, '请先上传或选择范围 GeoJSON')
        validate_area(area)
        request['area'] = str(area)
    for key in ('landcover_source', 'euluc_source'):
        request[key] = str(local_path(request[key])) if request[key] else str(SOURCES / ('landcover' if key.startswith('landcover') else 'euluc'))
    if 'landcover' in body.datasets and not list(Path(request['landcover_source']).glob('*.zip')):
        raise HTTPException(400, '土地覆盖数据源目录没有 ZIP 分卷，请先放入 sources/landcover 或选择源目录')
    if 'euluc' in body.datasets:
        if not body.euluc_member.lower().endswith('.shp') or '/' not in body.euluc_member:
            raise HTTPException(400, '城市成员格式应为 按城市划分.zip/按城市划分/城市.shp')
        if not (Path(request['euluc_source']) / body.euluc_member.split('/')[0]).is_file():
            raise HTTPException(400, '城市用地数据源 ZIP 不存在')
    if any(not re.fullmatch(r'EMSR\d+', code.upper()) for code in body.event_codes):
        raise HTTPException(400, '事件编号格式应为 EMSR927')
    request['event_codes'] = [s.upper() for s in body.event_codes]
    if not body.output.strip():
        raise HTTPException(400, '请选择结果保存目录')
    output = local_path(body.output.strip())
    # Keep generated products out of source code and caches.
    protected = [ROOT / x for x in ('backend','frontend','cache','sources','.runtime','.git','scripts','tests','docs')]
    if output == ROOT or any(output == path or path in output.parents for path in protected):
        raise HTTPException(400, '请选择 data 或项目源码目录之外的输出文件夹')
    try:
        output.mkdir(parents=True, exist_ok=True)
        request['output'] = str(output)
        # Snapshot the area so edits made while queued cannot change the task.
        if request.get('area'):
            import shutil
            snapshot = RUNTIME / 'areas' / uuid.uuid4().hex / Path(request['area']).name
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(request['area'], snapshot)
            request['area'] = str(snapshot)
        return manager.create(request)
    except OSError as exc:
        raise HTTPException(400, f'无法写入保存位置：{exc}')

@app.get('/api/jobs')
def jobs():
    with manager.lock:
        return sorted([dict(j) for j in manager.jobs.values()], key=lambda j: j['created_at'], reverse=True)

@app.get('/api/jobs/{key}')
def job_detail(key: str):
    job = get_job(key)
    path = RUNTIME / key / 'log.txt'
    if path.exists():
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 32000))
            job['log'] = stream.read().decode('utf-8', errors='replace')
    else:
        job['log'] = '任务已加入队列，等待开始。'
    return job

@app.post('/api/jobs/{key}/cancel')
def cancel_job(key: str):
    get_job(key)
    return manager.cancel(key)

@app.get('/api/jobs/{key}/files')
def job_files(key: str):
    job = get_job(key)
    output = Path(job['output'])
    if 'files' in job:
        return [dict(name=name, bytes=(output/name).stat().st_size) for name in job['files'] if (output/name).is_file()]
    return [dict(name=str(p.relative_to(output)).replace('\\','/'), bytes=p.stat().st_size) for p in output.rglob('*') if p.is_file() and not p.is_symlink()]

@app.get('/api/jobs/{key}/download/{name:path}')
def download(key: str, name: str):
    job = get_job(key)
    output = Path(job['output']).resolve()
    if 'files' in job and name not in job['files']:
        raise HTTPException(404, '文件不属于此任务')
    path = (output / name).resolve()
    if output not in path.parents or not path.is_file():
        raise HTTPException(404, '文件不存在')
    return FileResponse(path, filename=path.name)

dist = ROOT / 'frontend' / 'dist'
if dist.exists():
    app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
else:
    @app.get('/')
    def missing_frontend():
        return {'message':'请先运行 python scripts/setup.py，或在 frontend 中运行 npm install 与 npm run build。', 'docs':'/docs'}
