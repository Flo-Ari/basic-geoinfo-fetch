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
from array import array
from email.utils import parsedate_to_datetime

import requests
import osmium
import geopandas as gpd
from shapely.geometry import LineString, mapping, shape
from shapely.ops import polygonize, unary_union
from shapely.validation import make_valid

# 使脚本可被任意工作目录调用时仍能 import 同级配置文件
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import OSM_Hydro_config as cfg
script_dir = os.environ["GEOFETCH_WORK_DIR"]

# 命令行覆盖（不传参时与 config 默认值完全一致）
_parser = argparse.ArgumentParser(description="全国 OSM 水系裁剪提取")
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
    """由文件名模板构造月份标签正则：china-{time}.hydro.osm.pbf -> china-(\\d{4}-\\d{2})\\.hydro\\.osm\\.pbf"""
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


def is_water_area(tags):
    """判断标签是否为面状水体"""
    for k, v in cfg.WATER_AREA_RULES:
        if tags.get(k) == v:
            return True
    return False


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
HEADERS = {"User-Agent": "OSM-Hydro-Extract/1.0"}


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
    """将全国 PBF 中的水系要素流式转换为 GeoJSON（单行单要素，保留全部原始标签）。"""
    print("  开始转换全国 PBF -> GeoJSON...")

    # --- 第一遍：识别面状水体的 multipolygon 关系（不加载节点坐标，速度快） ---
    water_relations = {}   # rel_id -> (tags, [(role, way_ref), ...])
    member_way_ids = set()

    class RelationFinder(osmium.SimpleHandler):
        def relation(self, r):
            # 全国数据量巨大，先用标签快速排除非水体关系，避免为所有关系构建 tags 字典
            if r.tags.get("type") != "multipolygon":
                return
            if not any(r.tags.get(k) == v for k, v in cfg.WATER_AREA_RULES):
                return
            members = [(m.role, m.ref) for m in r.members if m.type == "w"]
            if not members:
                return
            water_relations[r.id] = ({t.k: t.v for t in r.tags if t.k in cfg.KEEP_KEYS}, members)
            for _, ref in members:
                member_way_ids.add(ref)

    print("  第一遍：识别面状水体 multipolygon 关系...")
    RelationFinder().apply_file(pbf_ascii)
    print(f"  发现 {len(water_relations)} 个面状水体关系, 涉及 {len(member_way_ids)} 条成员 way")

    # --- 第二遍：提取线状水系 / 面状水体闭合 way / 关系成员 way（带节点坐标） ---
    member_way_coords = {}   # way_id -> array('d')，[lon, lat, lon, lat, ...]

    class HydroWriter(osmium.SimpleHandler):
        def __init__(self, out):
            super().__init__()
            self.out = out
            self.count = 0

        def _write(self, geom_type, coordinates, tags):
            feature = {
                "type": "Feature",
                "geometry": {"type": geom_type, "coordinates": coordinates},
                "properties": tags,
            }
            self.out.write((json.dumps(feature, ensure_ascii=False) + ",\n").encode("utf-8"))
            self.count += 1
            if self.count % 1000000 == 0:
                print(f"  已写入 {self.count:,} 条要素...", flush=True)

        def way(self, w):
            is_member = w.id in member_way_ids
            area_hit = any(w.tags.get(k) == v for k, v in cfg.WATER_AREA_RULES)
            hv = w.tags.get("waterway")
            if not is_member and not area_hit and hv not in cfg.WATERWAY_LINE_VALUES:
                return
            try:
                coords = [[n.location.lon, n.location.lat] for n in w.nodes]
            except Exception:
                return  # 个别跨境 way 缺节点坐标，跳过
            if is_member:
                # 关系成员 way：记录扁平坐标数组，供第三步组装面状关系
                flat = array("d")
                for x, y in coords:
                    flat.append(x)
                    flat.append(y)
                member_way_coords[w.id] = flat
            n = len(coords)
            is_closed = n >= 4 and coords[0] == coords[-1]

            tags = {t.k: t.v for t in w.tags if t.k in cfg.KEEP_KEYS}
            # 面状水体（闭合 way）
            if area_hit and is_closed:
                self._write("Polygon", [coords[:-1]], tags)  # 去掉闭合重复点
                return
            # 线状水系（仅保留河流水系，排除 dam/weir/lock_gate 等结构类）
            if hv in cfg.WATERWAY_LINE_VALUES and n >= 2:
                self._write("LineString", coords, tags)

    print("  第二遍：提取水系要素（加载节点坐标）...")
    part_path = geojson_path + ".part"
    with open(part_path, "wb") as f:
        f.write(b'{"type": "FeatureCollection", "features": [\n')
        writer = HydroWriter(f)
        writer.apply_file(pbf_ascii, locations=True, idx="flex_mem")
        way_count = writer.count

        # --- 第三步：组装面状水体关系（multipolygon -> Polygon / MultiPolygon） ---
        print("  第三步：组装面状水体关系...")
        rel_count = 0
        rel_skipped = 0
        for rel_id, (tags, members) in water_relations.items():
            lines = []
            for _, ref in members:
                flat = member_way_coords.get(ref)
                if not flat:
                    continue
                pts = list(zip(flat[0::2], flat[1::2]))
                if len(pts) >= 2:
                    lines.append(LineString(pts))
            if not lines:
                continue
            polys = list(polygonize(lines))
            if not polys:
                continue
            geom = robust_union(polys)
            if geom is None or geom.is_empty:
                rel_skipped += 1
                continue
            feature = {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": tags,
            }
            f.write((json.dumps(feature, ensure_ascii=False) + ",\n").encode("utf-8"))
            rel_count += 1
            if rel_count % 50000 == 0:
                print(f"  已组装 {rel_count:,} 个关系...", flush=True)

        total = writer.count + rel_count
        if total == 0:
            f.write(b"]}\n")
        else:
            f.seek(-2, os.SEEK_END)  # 覆盖最后一行末尾的 ",\n"
            f.write(b"\n]}\n")
    os.replace(part_path, geojson_path)
    size_gb = os.path.getsize(geojson_path) / (1024 ** 3)
    print(f"  转换完成: {os.path.basename(geojson_path)}")
    print(f"    way 要素: {way_count:,} 个, 关系要素: {rel_count:,} 个, 合计 {total:,} 个 ({size_gb:.2f} GB)")
    if rel_skipped:
        print(f"    提示: {rel_skipped} 个关系因脏几何无法组装，已跳过")


def robust_union(polys):
    """unary_union 的容错版本：OSM 脏数据（自相交环等）引发拓扑异常时，
    逐个 make_valid 修复后再增量合并，仍失败的单个跳过；全部失败返回 None。"""
    try:
        return unary_union(polys)
    except Exception:
        pass
    geom = None
    for p in polys:
        try:
            if not p.is_valid:
                p = make_valid(p)
            if p.is_empty:
                continue
            geom = p if geom is None else geom.union(p)
        except Exception:
            continue
    return geom


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

records = []   # [(属性字典, shapely 几何), ...]
total = bbox_hits = invalid_skipped = 0

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
    try:
        inter = shape(geom_j).intersection(polygon)
    except Exception:
        # 脏几何（自相交环等）：修复后重试，仍失败则跳过
        try:
            inter = make_valid(shape(geom_j)).intersection(polygon)
        except Exception:
            invalid_skipped += 1
            continue
    if not inter.is_empty:
        props = {k: v for k, v in (feat.get("properties") or {}).items() if k in cfg.KEEP_KEYS}
        records.append((props, inter))

print(f"  扫描 {total:,} 条要素, bbox 命中 {bbox_hits:,} 条, 与目标范围相交 {len(records):,} 条")
if invalid_skipped:
    print(f"  提示: {invalid_skipped} 条脏几何无法修复，已跳过")

if not records:
    print("错误: 未提取到任何数据！")
    sys.exit(1)

gdf = gpd.GeoDataFrame(
    [{**p, "geometry": g} for p, g in records],
    geometry="geometry", crs="EPSG:4326",
)
gdf = gdf.explode(ignore_index=True)  # 将 MultiLineString / MultiPolygon 拆为单一几何
print(f"几何类型分布:\n{gdf.geometry.geom_type.value_counts().to_string()}")

out_dir = OUT_DIR if os.path.isabs(OUT_DIR) else os.path.join(script_dir, OUT_DIR)
os.makedirs(out_dir, exist_ok=True)
geojson_out = os.path.join(out_dir, OUT_FILENAME)
report("写入成果", force=True)
gdf.to_file(geojson_out, driver="GeoJSON")
report("写入成果", 1, 1, force=True)
size_mb = os.path.getsize(geojson_out) / (1024 * 1024)
clip_secs = time.time() - t_clip
print(f"\nGeoJSON 已保存: {geojson_out} ({size_mb:.1f} MB)")

print(f"\n=== {TARGET_NAME}（全国数据裁剪）水系统计 ===")
print(f"总数量: {len(gdf)}")
print(f"字段数: {len(gdf.columns)}")

# === 4. 耗时统计 ===
t_total = time.time() - t_start
print(f"\n=== 耗时统计 ===")
print(f"全国 PBF 准备: {pbf_secs:.1f} 秒 ({pbf_secs/60:.1f} 分钟)")
print(f"PBF -> 全国 GeoJSON 转换: {convert_secs:.1f} 秒 ({convert_secs/60:.1f} 分钟)")
print(f"流式裁剪: {clip_secs:.1f} 秒 ({clip_secs/60:.1f} 分钟)")
print(f"总耗时: {t_total:.1f} 秒 ({t_total/60:.1f} 分钟)")

print("\n全部完成!")
