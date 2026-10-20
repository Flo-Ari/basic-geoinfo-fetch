# -*- coding: utf-8 -*-
"""Provider adapted from the original project. Run through geofetch.worker."""

import os
import sys
import time
from geofetch.progress import report
import json
import math

import requests
import geopandas as gpd
import rasterio
from shapely.ops import polygonize, unary_union
from geofetch.processing import merge
from geofetch.processing import mask, write
from rasterio.io import MemoryFile

# 使脚本可被任意工作目录调用时仍能 import 同级配置文件
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import DEM_config as cfg
script_dir = os.environ["GEOFETCH_WORK_DIR"]

t_start = time.time()


def to_area_polygon(geom, buffer_deg):
    """把任意几何统一转为面：面原样返回；闭合线转成面；开放线/点按 buffer_deg 缓冲。"""
    gt = geom.geom_type
    if gt in ("Polygon", "MultiPolygon"):
        return geom
    if gt == "LineString":
        polys = list(polygonize([geom]))
        return unary_union(polys) if polys else geom.buffer(buffer_deg)
    if gt == "MultiLineString":
        polys = list(polygonize(list(geom.geoms)))
        return unary_union(polys) if polys else geom.buffer(buffer_deg)
    return geom.buffer(buffer_deg)


# === 1. 读取目标范围多边形 ===
area_path = cfg.AREA_GEOJSON
if not os.path.isabs(area_path):
    area_path = os.path.join(script_dir, area_path)
area_gdf = gpd.read_file(area_path).to_crs("EPSG:4326")
polygon = unary_union([to_area_polygon(g, cfg.AREA_BUFFER) for g in area_gdf.geometry])
minx, miny, maxx, maxy = polygon.bounds
print(f"目标范围: {area_path}（{len(area_gdf)} 个面要素）")
print(f"范围边界框: ({minx:.4f}, {miny:.4f}) - ({maxx:.4f}, {maxy:.4f})")

TARGET_NAME = os.path.splitext(os.path.basename(cfg.AREA_GEOJSON))[0]
OUT_FILENAME = cfg.OUT_FILENAME.format(TARGET_NAME=TARGET_NAME)


# === 2. 定位覆盖目标范围的 1°×1° 瓦片 ===
def tile_name(lat_floor, lon_floor):
    """根据瓦片左下角整数经纬度生成 Copernicus GLO-30 瓦片名。"""
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"{cfg.DEM_TILE_PREFIX}_{ns}{abs(lat_floor):02d}_00_{ew}{abs(lon_floor):03d}_00_DEM"


def tiles_covering(minx, miny, maxx, maxy):
    """返回覆盖边界框的所有 1°×1° 瓦片名（左下角整数经纬度）。"""
    # epsilon 避免恰好落在整度数边界时多取相邻瓦片
    EPS = 1e-9
    lat_start = math.floor(miny + EPS)
    lat_end = math.floor(maxy - EPS)
    lon_start = math.floor(minx + EPS)
    lon_end = math.floor(maxx - EPS)
    tiles = set()
    for lat in range(lat_start, lat_end + 1):
        for lon in range(lon_start, lon_end + 1):
            tiles.add(tile_name(lat, lon))
    return tiles


tiles = tiles_covering(minx, miny, maxx, maxy)
if not tiles:
    print("错误: 未匹配到任何 DEM 瓦片！")
    sys.exit(1)
print(f"\n目标范围覆盖 {len(tiles)} 个 DEM 瓦片:")
for t in sorted(tiles):
    print(f"  - {t}")


# === 3. 下载瓦片（断点续传 + 总进度） ===
cache_dir = os.path.join(script_dir, cfg.CACHE_DIR)
os.makedirs(cache_dir, exist_ok=True)

MAX_RETRIES = 20  # 网络中断自动续传的最大重试次数

_proxy = getattr(cfg, "PROXY", "").strip()
proxies = {"http": _proxy, "https": _proxy} if _proxy else None
HEADERS = {"User-Agent": "DEM-Extract/1.0"}


def remote_size(url):
    """返回远端文件字节数；无法获取时返回 0。"""
    try:
        r = requests.head(url, headers=HEADERS, timeout=30,
                          allow_redirects=True, proxies=proxies)
        cl = r.headers.get("content-length")
        if cl and cl.isdigit():
            return int(cl)
    except Exception:
        pass
    try:
        h = dict(HEADERS)
        h["Range"] = "bytes=0-0"
        r = requests.get(url, headers=h, timeout=30, stream=True, proxies=proxies)
        cr = r.headers.get("content-range") or ""
        if "/" in cr:
            n = cr.split("/")[-1].strip()
            if n.isdigit():
                return int(n)
        cl = r.headers.get("content-length")
        if cl and cl.isdigit():
            return int(cl)
    except Exception:
        pass
    return 0


# 预扫描：统计每个瓦片的总大小与已缓存字节，用于计算「总进度」
jobs = []  # (tile, url, total_size, done_initial, cached)
for t in sorted(tiles):
    final_path = os.path.join(cache_dir, f"{t}.tif")
    part_path = final_path + ".part"
    url = f"{cfg.DEM_BUCKET}/{t}/{t}.tif"
    if os.path.exists(final_path):
        sz = os.path.getsize(final_path)
        jobs.append((t, url, sz, sz, True))
    else:
        done = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        sz = remote_size(url)
        jobs.append((t, url, sz, done, False))

state = {"done": sum(j[3] for j in jobs),
         "total": sum(j[2] for j in jobs)}


def print_total(detail):
    d, tot = state["done"], state["total"]
    report("下载", d, tot or None, "字节")
    if tot > 0:
        print(f"\r  总进度: {d / tot * 100:5.1f}% "
              f"({d / (1024 * 1024):.0f}/{tot / (1024 * 1024):.0f} MB)  {detail}   ",
              end="", flush=True)
    else:
        print(f"\r  已下载: {d / (1024 * 1024):.0f} MB  {detail}   ", end="", flush=True)


def download_tile(tile, url, total_size):
    """下载单个瓦片到缓存目录，支持断点续传 + 网络中断自动重试。返回本地 .tif 路径。"""
    final_path = os.path.join(cache_dir, f"{tile}.tif")
    part_path = final_path + ".part"

    attempt = 0
    while True:
        attempt += 1
        existing = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        headers = dict(HEADERS)
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        try:
            resp = requests.get(url, stream=True, timeout=(30, 120),
                                headers=headers, proxies=proxies)

            if resp.status_code == 416:  # Range 越界：说明 .part 已完整
                os.rename(part_path, final_path)
                print(f"\n  [续传完成] {tile}.tif")
                return final_path
            if resp.status_code == 206:
                mode = "ab"
            elif resp.status_code == 200:
                mode = "wb"
                if existing > 0:
                    state["done"] -= existing  # 服务器不支持续传，已缓存字节作废
                    existing = 0
            else:
                resp.raise_for_status()

            # 预扫描未取到大小则用响应头补齐 total
            clen = resp.headers.get("content-length", "")
            if not total_size and clen.isdigit():
                state["total"] += int(clen)
                total_size = int(clen)

            downloaded = existing
            t_dl = time.time()

            with open(part_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    state["done"] += len(chunk)
                    elapsed = time.time() - t_dl
                    speed = (downloaded - existing) / (1024 * 1024) / elapsed if elapsed > 0 else 0
                    print_total(f"速度 {speed:.1f} MB/s")

            print()
            os.rename(part_path, final_path)
            print(f"  [下载完成] {tile}.tif ({os.path.getsize(final_path) / (1024 * 1024):.0f} MB)")
            return final_path

        except requests.exceptions.RequestException as e:
            saved = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            if attempt >= MAX_RETRIES:
                print(f"\n  [错误] 连续 {MAX_RETRIES} 次下载中断，已放弃。"
                      f"已缓存 {saved / (1024 * 1024):.0f} MB，重跑脚本可继续。\n  {e}")
                raise
            print(f"\n  [网络中断] 已缓存 {saved / (1024 * 1024):.0f} MB，"
                  f"5 秒后自动续传（第 {attempt}/{MAX_RETRIES} 次重试）...")
            time.sleep(5)


print("\n开始获取瓦片...")
tile_paths = []
for tile, url, total_size, _, cached in jobs:
    if cached:
        print(f"  [缓存命中] {tile}.tif")
        tile_paths.append(os.path.join(cache_dir, f"{tile}.tif"))
        continue
    print(f"  下载: {url}")
    tile_paths.append(download_tile(tile, url, total_size))

report("下载", state["done"], state["total"] or None, "字节", force=True)
if state["total"] > 0:
    print(f"\n  下载总进度: 100% ({state['total'] / (1024 * 1024):.0f} MB)")


# === 4. 拼接 + 裁剪到目标范围 ===
print("\n读取瓦片并裁剪到目标范围...")
geoms = [json.loads(gpd.GeoSeries([polygon], crs="EPSG:4326").to_json())["features"][0]["geometry"]]

datasets = [rasterio.open(p) for p in tile_paths]

if len(datasets) == 1:
    out_image, out_transform = mask(datasets[0], geoms, crop=True, nodata=cfg.NODATA)
    out_meta = datasets[0].meta.copy()
else:
    mosaic, mosaic_transform = merge(datasets, nodata=cfg.NODATA)

    profile = datasets[0].profile.copy()
    profile.update(height=mosaic.shape[1], width=mosaic.shape[2],
                   transform=mosaic_transform, count=mosaic.shape[0],
                   nodata=cfg.NODATA)

    with MemoryFile() as memfile:
        with memfile.open(**profile) as mds:
            mds.write(mosaic)
            out_image, out_transform = mask(mds, geoms, crop=True, nodata=cfg.NODATA)
    out_meta = datasets[0].meta.copy()

for ds in datasets:
    ds.close()

out_meta.update({
    "driver": "GTiff",
    "height": out_image.shape[1],
    "width": out_image.shape[2],
    "transform": out_transform,
    "nodata": cfg.NODATA,
    "compress": "lzw",
})

out_dir = os.path.join(script_dir, cfg.OUT_DIR)
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, OUT_FILENAME)
with rasterio.open(out_path, "w", **out_meta) as dst:
    write(dst, out_image)

size_mb = os.path.getsize(out_path) / (1024 * 1024)
print(f"\nDEM 已保存: {out_path} ({size_mb:.1f} MB)")

# === 5. 结果统计 ===
data = out_image[out_image != cfg.NODATA]
print(f"\n=== DEM 统计 ===")
print(f"尺寸: {out_meta['width']} x {out_meta['height']} 像素")
print(f"分辨率: {out_meta['transform'][0]:.6f}° (约 {out_meta['transform'][0]*111000:.0f} m)")
print(f"数据类型: {out_meta['dtype']}")
if data.size:
    print(f"高程范围: {data.min():.1f} - {data.max():.1f} m")
    print(f"高程均值: {data.mean():.1f} m")
print(f"无数据值: {cfg.NODATA}")

# === 6. 耗时统计 ===
t_total = time.time() - t_start
print(f"\n=== 耗时统计 ===")
print(f"总耗时: {t_total:.1f} 秒 ({t_total/60:.1f} 分钟)")

print("\n全部完成!")
