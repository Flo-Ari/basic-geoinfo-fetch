"""Run one provider in an isolated process, with explicit task-specific settings."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys

from .settings import CACHE, SOURCES, ROOT

PROVIDERS = Path(__file__).parent / 'providers'
ADAPTERS = {
    'dem': ('dem', 'DEM_config', 'DEM_Extract.py'),
    'population': ('population', 'POP_config', 'POP_Extract.py'),
    'roads': ('roads', 'OSM_Roads_config', 'OSM_Roads_Extract.py'),
    'hydro': ('hydro', 'OSM_Hydro_config', 'OSM_Hydro_Extract.py'),
    'landcover': ('landuse', 'Landuse_config', 'Landuse_SLC_Extract.py'),
    'euluc': ('landuse', 'Landuse_config', 'Landuse_EULUC_Extract.py'),
}

def run(dataset: str, request: dict):
    from .progress import report
    if dataset not in ADAPTERS and dataset not in {'events', 'medical'}:
        raise ValueError('不支持的数据类型')
    report('准备数据', force=True)
    output = Path(request['output']) / dataset
    output.mkdir(parents=True, exist_ok=True)
    cache = CACHE / dataset
    cache.mkdir(parents=True, exist_ok=True)
    if dataset == 'medical':
        from .providers.medical.medical import collect
        collect(request['area'], output, cache, CACHE)
        return
    if dataset == 'events':
        from .providers.events.client import CemsClient
        from .providers.events.downloader import CemsPdfDownloader
        # Product ZIPs are cache files; PDF products and manifest are task output.
        client = CemsClient()
        original = client.download_file
        def cached_download(url, destination, **kwargs):
            import hashlib
            import shutil
            target = cache / (hashlib.sha256(url.encode()).hexdigest() + '.zip')
            if not target.exists():
                original(url, target, **kwargs)
            else:
                print(f'[缓存命中] {target.name}', flush=True)
                report('下载', target.stat().st_size, target.stat().st_size, '字节', force=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, destination)
            return target.stat().st_size
        client.download_file = cached_download
        summary = CemsPdfDownloader(client, output).run(limit=request.get('event_limit', 3), codes=request.get('event_codes', []))
        if summary.failures:
            raise RuntimeError('部分事件下载失败，请查看 manifest.json')
        return
    folder, config_name, script = ADAPTERS[dataset]
    provider = PROVIDERS / folder
    sys.path.insert(0, str(provider))
    spec = importlib.util.spec_from_file_location(config_name, provider / (config_name + '.py'))
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    config.AREA_GEOJSON = request.get('area', '')
    config.OUT_DIR = str(output)
    config.CACHE_DIR = str(cache)
    config.PROXY = os.getenv('HTTPS_PROXY', '')
    config.DEM_OUT_DIR = str(output)
    config.DEM_CACHE_DIR = str(CACHE / 'dem')
    config.SCLC_OUT_DIR = str(output)
    config.SCLC_CACHE_DIR = str(cache)
    config.SCLC_ROOT = request.get('landcover_source') or str(SOURCES / 'landcover')
    config.EULUC_OUT_DIR = str(output)
    # Keep each selected city in a distinct cache, to avoid matching old city files.
    import hashlib
    config.EULUC_CACHE_DIR = str(cache / hashlib.sha256(request.get('euluc_member', '').encode()).hexdigest()[:12])
    config.EULUC_ROOT = request.get('euluc_source') or str(SOURCES / 'euluc')
    config.EULUC_ZIP_MEMBER = request.get('euluc_member', '按城市划分.zip/按城市划分/重庆市.shp')
    sys.modules[config_name] = config
    work = cache
    os.environ['GEOFETCH_WORK_DIR'] = str(work)
    os.chdir(work)
    sys.argv = [script]
    runpy.run_path(str(provider / script), run_name='__main__')

if __name__ == '__main__':
    payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    run(sys.argv[2], payload)
