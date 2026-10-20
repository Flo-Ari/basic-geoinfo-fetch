from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / '.env')
CACHE = ROOT / 'cache'
SOURCES = ROOT / 'sources'
DATA = ROOT / 'data'
RUNTIME = ROOT / '.runtime'
for directory in (CACHE, SOURCES / 'boundaries', SOURCES / 'landcover', SOURCES / 'euluc', DATA, RUNTIME):
    directory.mkdir(parents=True, exist_ok=True)

def local_path(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()

CATALOG = [
    dict(id='dem', name='数字高程', english='ELEVATION', description='Copernicus GLO-30 · 30m 地形栅格', format='GeoTIFF', mode='online', area=True, size='按 1° 瓦片下载'),
    dict(id='population', name='人口分布', english='POPULATION', description='WorldPop · 2026 中国 100m 人口栅格', format='GeoTIFF', mode='online', area=True, size='首次约 900 MB'),
    dict(id='roads', name='道路与铁路', english='TRANSPORT', description='OpenStreetMap · 道路分类与名称', format='GeoJSON', mode='online', area=True, size='全国 PBF 约 1.6 GB'),
    dict(id='hydro', name='河流与水体', english='HYDROGRAPHY', description='OpenStreetMap · 河流、湖泊与水库', format='GeoJSON', mode='online', area=True, size='全国 PBF 约 1.6 GB'),
    dict(id='landcover', name='土地覆盖', english='LAND COVER', description='中国南方 · 2020 年 1.5m 分类栅格', format='GeoTIFF', mode='local', area=True, size='需要本地分卷 ZIP'),
    dict(id='euluc', name='城市用地', english='URBAN LAND', description='EULUC · 2018 年城市用地类型', format='GeoJSON', mode='local', area=True, size='需要本地城市 ZIP'),
    dict(id='events', name='灾害事件地图', english='EMERGENCY', description='Copernicus EMS · 公开灾害事件 PDF', format='PDF', mode='online', area=False, size='按事件产品包下载'),
]
