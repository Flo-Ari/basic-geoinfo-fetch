# -*- coding: utf-8 -*-
"""Provider adapted from the original project. Run through geofetch.worker."""

import os
import sys
import time
from geofetch.progress import report, track
import zipfile

# 环境自修正：系统 PROJ_LIB/GDAL_DATA 若指向 PostgreSQL 自带的旧版库，
# 会与 GDAL 内置 PROJ 冲突（proj.db layout 版本过旧），需在导入前移除
for _var in ("PROJ_LIB", "GDAL_DATA"):
    _val = os.environ.get(_var, "")
    if "Postgresql" in _val or "postgis" in _val:
        os.environ.pop(_var, None)

import geopandas as gpd
from shapely.ops import polygonize, unary_union
from shapely.validation import make_valid

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import Landuse_config as cfg
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
print(f"目标范围: {area_path}（{len(area_gdf)} 个要素）")
print(f"范围边界框: ({minx:.4f}, {miny:.4f}) - ({maxx:.4f}, {maxy:.4f})")

TARGET_NAME = os.path.splitext(os.path.basename(cfg.AREA_GEOJSON))[0]

# === 2. 从 zip 抽出城市 shp 到缓存 ===
zip_name, _, member = cfg.EULUC_ZIP_MEMBER.partition("/")
zip_path = os.path.join(cfg.EULUC_ROOT, zip_name)
if not os.path.exists(zip_path):
    print(f"错误: 数据源不存在: {zip_path}")
    sys.exit(1)

cache_dir = os.path.join(script_dir, cfg.EULUC_CACHE_DIR)
os.makedirs(cache_dir, exist_ok=True)
stem, _ = os.path.splitext(member)
cache_shp = os.path.join(cache_dir, os.path.basename(member))

with zipfile.ZipFile(zip_path) as zf:
    # Some legacy archives contain UTF-8 names without the ZIP UTF-8 flag.
    for info in zf.infolist():
        if not info.flag_bits & 0x800:
            try:
                corrected = info.filename.encode('cp437').decode('utf-8')
            except (UnicodeError, LookupError):
                continue
            zf.NameToInfo.pop(info.filename, None)
            info.filename = corrected
            zf.NameToInfo[corrected] = info
    sidecars = [n for n in zf.namelist()
                if n.startswith(stem + ".") and n[len(stem):] in (".cpg", ".dbf", ".prj", ".shp", ".shx")]
    if not all(any(n.endswith(ext) for n in sidecars) for ext in ('.shp', '.shx', '.dbf')):
        raise ValueError(f'ZIP 中缺少 {member} 或必要的 .shx/.dbf 伴随文件')
    missing = [os.path.basename(n) for n in sidecars
               if not os.path.exists(os.path.join(cache_dir, os.path.basename(n)))]
    for n in sidecars:
        dest = os.path.join(cache_dir, os.path.basename(n))
        if not os.path.exists(dest):
            with open(dest, "wb") as f:
                f.write(zf.read(n))
if missing:
    print(f"已从 {zip_name} 抽出 {len(missing)} 个文件到 {cfg.EULUC_CACHE_DIR}/")
else:
    print(f"[缓存命中] {cfg.EULUC_CACHE_DIR}/{os.path.basename(member)}")

# === 3. 读取并裁剪到目标范围 ===
city_gdf = gpd.read_file(cache_shp)
print(f"\n城市图层: {member}（{len(city_gdf)} 个要素, {city_gdf.crs}）")

bbox_hits = city_gdf.cx[minx:maxx, miny:maxy]
print(f"bbox 预筛命中 {len(bbox_hits)} 个要素")

records = []
invalid_skipped = 0
for idx, row in track(bbox_hits.iterrows(), len(bbox_hits)):
    geom = row.geometry
    try:
        inter = geom.intersection(polygon)
    except Exception:
        try:
            inter = make_valid(geom).intersection(polygon)
        except Exception:
            invalid_skipped += 1
            continue
    if inter.is_empty:
        continue
    records.append(({k: v for k, v in row.drop("geometry").items()}, inter))

if invalid_skipped:
    print(f"提示: {invalid_skipped} 个脏几何无法修复，已跳过")
if not records:
    print("错误: 目标范围内没有城市用地要素（该数据仅覆盖城市建成区，山林火场无数据属正常）")
    sys.exit(1)

out_gdf = gpd.GeoDataFrame(
    [{**p, "geometry": g} for p, g in records],
    geometry="geometry", crs="EPSG:4326",
)

# 删除所有要素中值全为 null 的字段
before_cols = len(out_gdf.columns)
out_gdf = out_gdf.dropna(axis=1, how="all")
dropped = before_cols - len(out_gdf.columns)
if dropped:
    print(f"删除全 null 字段 {dropped} 个，保留 {len(out_gdf.columns)} 个字段")

# === 4. 输出 ===
out_dir = cfg.EULUC_OUT_DIR if os.path.isabs(cfg.EULUC_OUT_DIR) \
    else os.path.join(script_dir, cfg.EULUC_OUT_DIR)
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, cfg.EULUC_OUT_FILENAME.format(TARGET_NAME=TARGET_NAME))
report("写入成果", force=True)
out_gdf.to_file(out_path, driver="GeoJSON")
report("写入成果", 1, 1, force=True)
size_mb = os.path.getsize(out_path) / 1024 / 1024
print(f"\nGeoJSON 已保存: {out_path} ({size_mb:.2f} MB)")

# === 5. 结果统计 ===
print(f"\n=== {TARGET_NAME} 城市用地类型统计 ===")
print(f"总要素数: {len(out_gdf)}")
area_km2 = out_gdf.to_crs("EPSG:3857").geometry.area.sum() / 1e6
print(f"裁剪后总面积: {area_km2:.2f} km²")
if "Level1_cn" in out_gdf.columns:
    print("\n一级分类分布:")
    print(out_gdf["Level1_cn"].value_counts().to_string())
if "Level2_cn" in out_gdf.columns:
    print("\n二级分类分布（前 15）:")
    print(out_gdf["Level2_cn"].value_counts().head(15).to_string())

t_total = time.time() - t_start
print(f"\n=== 耗时统计 ===")
print(f"总耗时: {t_total:.1f} 秒 ({t_total/60:.1f} 分钟)")
print("\n全部完成!")
