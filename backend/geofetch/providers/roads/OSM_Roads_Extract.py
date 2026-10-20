# -*- coding: utf-8 -*-
"""Provider adapted from the original project. Run through geofetch.worker."""

import os
import re
import sys
import json
import glob
import time
from geofetch.progress import report
import shutil
import tempfile
import argparse
from email.utils import parsedate_to_datetime

import requests
import osmium
import geopandas as gpd
from shapely.geometry import shape
from shapely.ops import polygonize, unary_union

# 使脚本可被任意工作目录调用时仍能 import 同级配置文件
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import OSM_Roads_config as cfg
script_dir = os.environ["GEOFETCH_WORK_DIR"]

# 命令行覆盖（不传参时与 config 默认值完全一致）
_parser = argparse.ArgumentParser(description="全国 OSM 道路+铁路裁剪提取")
_parser.add_argument("--area", default=None,
                     help="目标范围 GeoJSON 路径（默认取 config 的 AREA_GEOJSON）")
_parser.add_argument("--out-dir", default=None,
                     help="输出目录，绝对或相对模块目录（默认取 config 的 OUT_DIR）")
_parser.add_argument("--padding", type=float, default=None,
                     help="bbox 外扩度数（默认取 config 的 AREA_PADDING）")
_args = _parser.parse_args()
AREA_GEOJSON = _args.area if _args.area else cfg.AREA_GEOJSON
OUT_DIR = _args.out_dir if _args.out_dir else cfg.OUT_DIR
AREA_PADDING = cfg.AREA_PADDING if _args.padding is None else _args.padding

t_start = time.time()


def tmpl_tag_re(tmpl):
    """由文件名模板构造月份标签正则：china-{time}.road.osm.pbf -> china-(\\d{4}-\\d{2})\\.road\\.osm\\.pbf"""
    return re.compile(re.escape(tmpl).replace(re.escape("{time}"), r"(\d{4}-\d{2})"))


PBF_TAG_RE = tmpl_tag_re(cfg.NATIONAL_PBF_TMPL)
GEOJSON_TAG_RE = tmpl_tag_re(cfg.NATIONAL_GEOJSON_TMPL)


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
area_path = AREA_GEOJSON
if not os.path.isabs(area_path):
    area_path = os.path.join(script_dir, area_path)
area_gdf = gpd.read_file(area_path).to_crs("EPSG:4326")
polygon = unary_union([to_area_polygon(g, cfg.AREA_BUFFER) for g in area_gdf.geometry])
minx, miny, maxx, maxy = polygon.bounds
print(f"目标范围: {area_path}（{len(area_gdf)} 个要素）")
print(f"范围边界框: ({minx:.4f}, {miny:.4f}) - ({maxx:.4f}, {maxy:.4f})")

TARGET_NAME = os.path.splitext(os.path.basename(AREA_GEOJSON))[0]
OUT_FILENAME = cfg.OUT_FILENAME.format(TARGET_NAME=TARGET_NAME)


MAX_RETRIES = 20  # 网络中断自动续传的最大重试次数
_proxy = getattr(cfg, "PROXY", "").strip()
proxies = {"http": _proxy, "https": _proxy} if _proxy else None
HEADERS = {"User-Agent": "OSM-Roads-Extract/1.0"}


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


def download_file(url, dest_path, headers, state, max_retries=MAX_RETRIES):
    """断点续传下载：先下到 dest_path.part，成功后重命名为 dest_path。返回是否成功。"""
    part_path = dest_path + ".part"
    for attempt in range(1, max_retries + 1):
        offset = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        hdr = dict(headers)
        if offset > 0:
            hdr["Range"] = f"bytes={offset}-"

        try:
            resp = requests.get(url, stream=True, timeout=60, headers=hdr, proxies=proxies)

            if resp.status_code == 416:  # Range 越界：.part 已完整
                os.rename(part_path, dest_path)
                return True
            if resp.status_code == 200:
                if offset > 0:
                    state["done"] -= offset  # 服务器不支持断点续传，已缓存字节作废
                    offset = 0
                mode = "wb"
            elif resp.status_code == 206:
                mode = "ab"
            else:
                resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"\n    下载中断（第 {attempt}/{max_retries} 次）: {e}")
            time.sleep(1)
            continue

        downloaded = offset
        start = time.time()

        try:
            with open(part_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    state["done"] += len(chunk)
                    report("下载", state["done"], state["total"] or None, "字节")
                    elapsed = time.time() - start
                    speed = (downloaded - offset) / (1024 * 1024) / elapsed if elapsed > 0 else 0
                    d, tot = state["done"], state["total"]
                    if tot > 0:
                        print(f"\r  总进度: {d / tot * 100:5.1f}% "
                              f"({d / (1024*1024):.0f}/{tot / (1024*1024):.0f} MB)  速度 {speed:.1f} MB/s  ",
                              end="", flush=True)
                    else:
                        print(f"\r  已下载: {d / (1024*1024):.0f} MB  速度 {speed:.1f} MB/s  ",
                              end="", flush=True)
        except requests.exceptions.RequestException as e:
            print(f"\n    下载中断（第 {attempt}/{max_retries} 次）: {e}")
            time.sleep(1)
            continue

        os.rename(part_path, dest_path)
        return True

    return False


def month_from_headers(url):
    """通过 HEAD 请求读取远端 Last-Modified，返回月份标签（如 2026-09）；失败返回 None。"""
    try:
        r = requests.head(url, headers=HEADERS, timeout=30,
                          allow_redirects=True, proxies=proxies)
        lm = r.headers.get("last-modified")
        if lm:
            return parsedate_to_datetime(lm).strftime("%Y-%m")
    except Exception:
        pass
    return None


def find_latest_national(tmpl, tag_re):
    """在本目录根目录查找符合命名模板的最新全国文件，返回 (路径, 月份标签) 或 (None, None)。"""
    pattern = os.path.join(script_dir, tmpl.format(time="????-??"))
    best = None
    for p in glob.glob(pattern):
        m = tag_re.fullmatch(os.path.basename(p))
        if not m or not os.path.exists(p) or os.path.getsize(p) == 0:
            continue
        key = (m.group(1), os.path.getmtime(p))
        if best is None or key > best[0]:
            best = (key, p, m.group(1))
    if best:
        return best[1], best[2]
    return None, None


def ascii_path(path):
    """路径含非 ASCII 字符（如项目放在中文目录下）时，pyosmium 的 C++ 底层可能无法打开，
    通过系统临时目录（纯 ASCII）建立副本解决；同卷优先硬链接（瞬时），跨卷回退为复制。"""
    if path.isascii():
        return path
    tmp = os.path.join(tempfile.gettempdir(), os.path.basename(path))
    # 临时副本已存在但比源文件旧（如换月重新下载后）时，同步为最新版本
    need_sync = (not os.path.exists(tmp) or os.path.getmtime(tmp) != os.path.getmtime(path))
    if need_sync:
        if os.path.exists(tmp):
            os.remove(tmp)
        try:
            os.link(path, tmp)
        except OSError:
            shutil.copy2(path, tmp)
        print(f"  路径含非 ASCII 字符，已建立临时副本: {tmp}")
    return tmp


def prepare_pbf(state):
    """查找/下载全国 PBF，返回 (pyosmium可用路径, 月份标签, 本地原始路径)；失败返回 (None, None, None)。"""
    pbf_path, tag = find_latest_national(cfg.NATIONAL_PBF_TMPL, PBF_TAG_RE)
    if pbf_path:
        print(f"  检测到本地全国 PBF: {os.path.basename(pbf_path)}")
        return ascii_path(pbf_path), tag, pbf_path

    for url in cfg.PBF_MIRRORS:
        print(f"  下载地址: {url}")
        tag = month_from_headers(url) or time.strftime("%Y-%m")
        dest = os.path.join(script_dir, cfg.NATIONAL_PBF_TMPL.format(time=tag))
        if os.path.exists(dest) and os.path.getsize(dest) == 0:
            os.remove(dest)  # 清理 0 字节残留文件
        if download_file(url, dest, dict(HEADERS), state):
            return ascii_path(dest), tag, dest
    print("  错误: 所有镜像均下载失败（全国 PBF），请检查网络。")
    return None, None, None


def convert_pbf_to_geojson(pbf_ascii, geojson_path):
    """将全国 PBF 中的道路/铁路 way 流式转换为 GeoJSON（单行单要素，保留全部原始标签）。"""
    print("  开始转换全国 PBF -> GeoJSON（保留 highway/railway 原始标签，供裁剪时翻译）...")

    class RoadWriter(osmium.SimpleHandler):
        def __init__(self, out):
            super().__init__()
            self.out = out
            self.count = 0

        def way(self, w):
            if w.tags.get("highway") is None and w.tags.get("railway") is None:
                return
            try:
                coords = [[n.location.lon, n.location.lat] for n in w.nodes]
            except Exception:
                return  # 个别跨境 way 缺节点坐标，跳过
            if len(coords) < 2:
                return
            feature = {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {t.k: t.v for t in w.tags if t.k in cfg.KEEP_KEYS},
            }
            self.out.write((json.dumps(feature, ensure_ascii=False) + ",\n").encode("utf-8"))
            self.count += 1
            if self.count % 1000000 == 0:
                print(f"  已写入 {self.count:,} 条要素...", flush=True)

    part_path = geojson_path + ".part"
    with open(part_path, "wb") as f:
        f.write(b'{"type": "FeatureCollection", "features": [\n')
        writer = RoadWriter(f)
        writer.apply_file(pbf_ascii, locations=True, idx="flex_mem")
        if writer.count == 0:
            f.write(b"]}\n")
        else:
            f.seek(-2, os.SEEK_END)  # 覆盖最后一行末尾的 ",\n"
            f.write(b"\n]}\n")
    os.replace(part_path, geojson_path)
    size_gb = os.path.getsize(geojson_path) / (1024 ** 3)
    print(f"  转换完成: {os.path.basename(geojson_path)}（{writer.count:,} 条要素, {size_gb:.2f} GB）")


def iter_features(geojson_path):
    """逐行读取单行单要素 GeoJSON，产出 Feature 字典（跳过首尾结构行）。"""
    with open(geojson_path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.endswith("["):
                continue
            if s.endswith(","):
                s = s[:-1]
            if not s.startswith("{"):
                continue
            try:
                obj = json.loads(s)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "Feature":
                yield obj


def geom_bbox(geom):
    """快速计算 GeoJSON 几何的包围盒 (minx, miny, maxx, maxy)，用于粗筛。"""
    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    def scan(node):
        nonlocal minx, miny, maxx, maxy
        if isinstance(node[0], (int, float)):
            x, y = node[0], node[1]
            if x < minx:
                minx = x
            if x > maxx:
                maxx = x
            if y < miny:
                miny = y
            if y > maxy:
                maxy = y
        else:
            for sub in node:
                scan(sub)

    scan(geom.get("coordinates"))
    return minx, miny, maxx, maxy


# === 2. 准备全国数据（PBF -> 全国 GeoJSON） ===
state = {"done": 0, "total": 0}
pbf_root = os.path.join(script_dir, cfg.NATIONAL_PBF_TMPL.format(time="????-??"))
for p in glob.glob(pbf_root):
    if os.path.getsize(p) > 0:
        state["done"] += os.path.getsize(p)
        state["total"] += os.path.getsize(p)
if state["total"] == 0:
    for url in cfg.PBF_MIRRORS:
        sz = remote_size(url)
        if sz:
            state["total"] += sz
            break

print("\n[1/3] 准备全国 PBF...")
t_pbf = time.time()
pbf_ascii, tag, pbf_orig = prepare_pbf(state)
if pbf_ascii is None:
    print("错误: 全国 PBF 获取失败，无法继续。")
    sys.exit(1)
pbf_secs = time.time() - t_pbf

print("\n[2/3] 准备全国 GeoJSON...")
t_conv = time.time()
geojson_path, geo_tag = find_latest_national(cfg.NATIONAL_GEOJSON_TMPL, GEOJSON_TAG_RE)
if geojson_path is not None and geo_tag == tag \
        and os.path.getmtime(geojson_path) >= os.path.getmtime(pbf_orig):
    print(f"  [缓存命中] {os.path.basename(geojson_path)}")
else:
    geojson_path = os.path.join(script_dir, cfg.NATIONAL_GEOJSON_TMPL.format(time=tag))
    report("转换 PBF", force=True)
    convert_pbf_to_geojson(pbf_ascii, geojson_path)
convert_secs = time.time() - t_conv
size_gb = os.path.getsize(geojson_path) / (1024 ** 3)
print(f"  使用全国 GeoJSON: {os.path.basename(geojson_path)} ({size_gb:.2f} GB)")


# === 3. 流式裁剪全国 GeoJSON 到目标范围 ===
print("\n[3/3] 裁剪到目标范围...")
t_clip = time.time()
south = miny - AREA_PADDING
west = minx - AREA_PADDING
north = maxy + AREA_PADDING
east = maxx + AREA_PADDING

records = []           # [(属性字典, shapely 几何), ...]
unmapped_highway = set()
total = bbox_hits = 0

from geofetch.spatial import indexed_features
for feat in indexed_features(geojson_path, (west, south, east, north)):
    total += 1
    if total % 2000000 == 0:
        print(f"  已扫描 {total:,} 条要素（命中 {len(records):,}）...", flush=True)
    geom_j = feat.get("geometry")
    if not geom_j:
        continue
    bb = geom_bbox(geom_j)
    if bb[2] < west or bb[0] > east or bb[3] < south or bb[1] > north:
        continue
    bbox_hits += 1
    inter = shape(geom_j).intersection(polygon)
    if inter.is_empty:
        continue

    # 统一 way 字段（与原逻辑一致：highway / railway 分别翻译，两者都有则生成两条要素）
    props = {k: v for k, v in (feat.get("properties") or {}).items() if k in cfg.KEEP_KEYS}
    hv = props.pop("highway", None)
    rv = props.pop("railway", None)
    if hv is not None and hv not in cfg.WAY_DROP:
        zh = cfg.HIGHWAY_ZH.get(hv)
        if zh is None:
            unmapped_highway.add(hv)
            zh = cfg.HIGHWAY_DEFAULT
        records.append(({**props, "way": zh}, inter))
    if rv is not None and rv not in cfg.WAY_DROP:
        records.append(({**props, "way": cfg.RAILWAY_ZH.get(rv, "铁路")}, inter))

print(f"  扫描 {total:,} 条要素, bbox 命中 {bbox_hits:,} 条, 与目标范围相交 {len(records):,} 条")

if unmapped_highway:
    print(f"提示: 以下 highway 值未在映射表中，已归为「{cfg.HIGHWAY_DEFAULT}」: {sorted(unmapped_highway)}")

if not records:
    print("错误: 未提取到任何数据！")
    sys.exit(1)

gdf = gpd.GeoDataFrame(
    [{**p, "geometry": g} for p, g in records],
    geometry="geometry", crs="EPSG:4326",
)
gdf = gdf.explode(ignore_index=True)  # 将 MultiLineString 拆为 LineString

# 删除所有要素中值全为 null 的字段
before_cols = len(gdf.columns)
gdf = gdf.dropna(axis=1, how="all")
dropped_cols = before_cols - len(gdf.columns)
print(f"删除全 null 字段 {dropped_cols} 个，保留 {len(gdf.columns)} 个字段")

out_dir = OUT_DIR if os.path.isabs(OUT_DIR) else os.path.join(script_dir, OUT_DIR)
os.makedirs(out_dir, exist_ok=True)
geojson_out = os.path.join(out_dir, OUT_FILENAME)
report("写入成果", force=True)
gdf.to_file(geojson_out, driver="GeoJSON")
report("写入成果", 1, 1, force=True)
size_mb = os.path.getsize(geojson_out) / (1024 * 1024)
clip_secs = time.time() - t_clip
print(f"\nGeoJSON 已保存: {geojson_out} ({size_mb:.1f} MB)")

print(f"\n=== {TARGET_NAME}（全国数据裁剪）道路+铁路统计 ===")
print(f"总数量: {len(gdf)}")
print(f"\nway 字段类型分布:")
print(gdf["way"].value_counts().to_string())

# === 4. 耗时统计 ===
t_total = time.time() - t_start
print(f"\n=== 耗时统计 ===")
print(f"全国 PBF 准备: {pbf_secs:.1f} 秒 ({pbf_secs/60:.1f} 分钟)")
print(f"PBF -> 全国 GeoJSON 转换: {convert_secs:.1f} 秒 ({convert_secs/60:.1f} 分钟)")
print(f"流式裁剪: {clip_secs:.1f} 秒 ({clip_secs/60:.1f} 分钟)")
print(f"总耗时: {t_total:.1f} 秒 ({t_total/60:.1f} 分钟)")

print("\n全部完成!")
