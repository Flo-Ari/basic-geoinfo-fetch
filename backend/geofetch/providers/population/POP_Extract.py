# -*- coding: utf-8 -*-
"""Provider adapted from the original project. Run through geofetch.worker."""

import os
import sys
import time
from geofetch.progress import report
import json
from ftplib import FTP, all_errors
from urllib.parse import urlparse

import geopandas as gpd
import rasterio
from shapely.ops import polygonize, unary_union
from geofetch.processing import mask, write

# 使脚本可被任意工作目录调用时仍能 import 同级配置文件
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import POP_config as cfg
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


# === 2. 下载全国人口栅格（支持断点续传） ===
# 全国栅格保存在本目录（POP/）根目录下；metaDEM/ 目录仅存放下载中的 .part 临时文件
cache_dir = os.path.join(script_dir, cfg.CACHE_DIR)
os.makedirs(cache_dir, exist_ok=True)

final_path = os.path.join(script_dir, os.path.basename(cfg.POP_URL))
part_path = os.path.join(cache_dir, os.path.basename(cfg.POP_URL) + ".part")


MAX_RETRIES = 20  # 网络中断自动续传的最大重试次数


def download_pop():
    """通过 FTP 下载全国人口栅格到本目录根目录，支持断点续传 + 网络中断自动重试。返回本地 .tif 路径。"""
    if os.path.exists(final_path):
        print(f"[缓存命中] {final_path}")
        return final_path

    parsed = urlparse(cfg.POP_URL)
    host = parsed.hostname
    remote_path = parsed.path  # 形如 /GIS/.../file.tif

    print(f"下载地址: {cfg.POP_URL}")

    attempt = 0
    while True:
        attempt += 1
        existing = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        ftp = None
        try:
            ftp = FTP(host, timeout=60)
            ftp.login()  # 匿名登录

            total = ftp.size(remote_path)  # 服务器上的完整文件大小
            if existing >= total:
                ftp.quit()
                os.rename(part_path, final_path)
                print("[续传完成]")
                return final_path

            if existing > 0:
                print(f"从 {existing/(1024*1024):.0f} MB 处续传...")

            mode = "ab" if existing > 0 else "wb"
            state = {"downloaded": existing, "last": time.time()}
            t_dl = time.time()

            with open(part_path, mode) as f:
                def cb(data):
                    f.write(data)
                    state["downloaded"] += len(data)
                    now = time.time()
                    if now - state["last"] > 0.5:
                        state["last"] = now
                        elapsed = now - t_dl
                        speed = (state["downloaded"] - existing) / (1024 * 1024) / elapsed if elapsed > 0 else 0
                        report("下载", state["downloaded"], total, "字节")
                        pct = state["downloaded"] / total * 100
                        print(f"\r  总进度: {pct:.0f}% ({state['downloaded']/(1024*1024):.0f}/{total/(1024*1024):.0f} MB) "
                              f"速度: {speed:.1f} MB/s  ", end="", flush=True)

                ftp.retrbinary(f"RETR {remote_path}", cb, rest=existing)

            ftp.quit()
            os.rename(part_path, final_path)
            print(f"\n[下载完成] {os.path.getsize(final_path)/(1024*1024):.0f} MB")
            return final_path

        except all_errors as e:
            if ftp is not None:
                try:
                    ftp.close()
                except Exception:
                    pass
            saved = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            if attempt >= MAX_RETRIES:
                print(f"\n[错误] 连续 {MAX_RETRIES} 次下载中断，已放弃。"
                      f"已缓存 {saved/(1024*1024):.0f} MB，重跑脚本可继续。\n{e}")
                raise
            print(f"\n[网络中断] 已缓存 {saved/(1024*1024):.0f} MB，"
                  f"5 秒后自动续传（第 {attempt}/{MAX_RETRIES} 次重试）...")
            time.sleep(5)


print("\n开始获取全国人口栅格（约 878 MB，首次下载耗时较长）...")
pop_path = download_pop()
report("下载", os.path.getsize(pop_path), os.path.getsize(pop_path), "字节", force=True)


# === 3. 裁剪到目标范围 ===
print("\n裁剪到目标范围...")
geoms = [json.loads(gpd.GeoSeries([polygon], crs="EPSG:4326").to_json())["features"][0]["geometry"]]

with rasterio.open(pop_path) as src:
    src_nodata = src.nodatavals[0]
    out_image, out_transform = mask(src, geoms, crop=True, nodata=cfg.NODATA)
    out_meta = src.meta.copy()
    src_crs = src.crs

# 将源栅格自身的无数据值统一替换为本项目的 NODATA，避免出现两种无数据值
if src_nodata is not None and src_nodata != cfg.NODATA:
    out_image[out_image == src_nodata] = cfg.NODATA

out_meta.update({
    "driver": "GTiff",
    "height": out_image.shape[1],
    "width": out_image.shape[2],
    "transform": out_transform,
    "crs": src_crs,
    "nodata": cfg.NODATA,
    "compress": "lzw",
})

out_dir = os.path.join(script_dir, cfg.OUT_DIR)
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, OUT_FILENAME)
with rasterio.open(out_path, "w", **out_meta) as dst:
    write(dst, out_image)

size_mb = os.path.getsize(out_path) / (1024 * 1024)
print(f"\n人口栅格已保存: {out_path} ({size_mb:.1f} MB)")

# === 4. 结果统计 ===
data = out_image[out_image != cfg.NODATA]
total_pop = float(data.sum()) if data.size else 0.0
print(f"\n=== 人口统计 ===")
print(f"尺寸: {out_meta['width']} x {out_meta['height']} 像素")
print(f"分辨率: {out_meta['transform'][0]:.6f}° (约 {out_meta['transform'][0]*111000:.0f} m)")
print(f"数据类型: {out_meta['dtype']}")
if data.size:
    print(f"人口总数: {total_pop:,.0f} 人")
    print(f"单像素最大值: {data.max():.1f} 人")

# === 5. 耗时统计 ===
t_total = time.time() - t_start
print(f"\n=== 耗时统计 ===")
print(f"总耗时: {t_total:.1f} 秒 ({t_total/60:.1f} 分钟)")

print("\n全部完成!")
