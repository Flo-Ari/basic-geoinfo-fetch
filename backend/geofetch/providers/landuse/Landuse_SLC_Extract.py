# -*- coding: utf-8 -*-
"""Provider adapted from the original project. Run through geofetch.worker."""

import io
import os
import sys
import time
from geofetch.progress import report
import json
import zipfile

# 环境自修正：系统 PROJ_LIB/GDAL_DATA 若指向 PostgreSQL 自带的旧版库，
# 会与 rasterio 内置 PROJ 冲突（proj.db layout 版本过旧），需在导入前移除
for _var in ("PROJ_LIB", "GDAL_DATA"):
    _val = os.environ.get(_var, "")
    if "Postgresql" in _val or "postgis" in _val:
        os.environ.pop(_var, None)

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.crs import CRS
from geofetch.processing import mask, write
from geofetch.processing import merge
from rasterio.io import MemoryFile
from shapely.ops import polygonize, unary_union

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import Landuse_config as cfg
script_dir = os.environ["GEOFETCH_WORK_DIR"]

t_start = time.time()
EPSG_MERC = CRS.from_epsg(cfg.SCLC_TARGET_EPSG)

# 分类调色板（写入 tif 颜色表，QGIS 打开即按类别着色；无数据 25 透明）
SCLC_CMAP = {
    0:  (34, 139, 34, 255),    # Tree cover 乔木/林地
    1:  (255, 0, 0, 255),      # Build-up 建成区
    2:  (189, 183, 107, 255),  # Shrubland and Grassland 灌草地
    3:  (240, 240, 255, 255),  # Snow and Ice 冰雪
    4:  (255, 255, 0, 255),    # Cropland 耕地
    5:  (0, 0, 255, 255),      # Water 水体
    6:  (197, 145, 90, 255),   # Bare or sparse vegetation 裸地/稀疏植被
    7:  (128, 128, 128, 255),  # Others 其他
    25: (0, 0, 0, 0),          # nodata 无数据（透明）
}

# 分类中文名。tif 内嵌颜色表只能存 RGB（GeoTIFF 无 RAT 类别名能力），
# 中文类别名靠同目录 .qml 侧车文件的 paletteEntry label 补全（QGIS 自动加载）
SCLC_CLASS_NAMES = {
    0:  "乔木/林地",
    1:  "建成区",
    2:  "灌草地",
    3:  "冰雪",
    4:  "耕地",
    5:  "水体",
    6:  "裸地/稀疏植被",
    7:  "其他",
    25: "无数据",
}


def to_area_polygon(geom, buffer_deg):
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


# === 1. 读取目标范围并转 EPSG:3857 ===
area_path = cfg.AREA_GEOJSON
if not os.path.isabs(area_path):
    area_path = os.path.join(script_dir, area_path)
area_gdf = gpd.read_file(area_path).to_crs("EPSG:4326")
poly4326 = unary_union([to_area_polygon(g, cfg.AREA_BUFFER) for g in area_gdf.geometry])
poly_merc = gpd.GeoSeries([poly4326], crs="EPSG:4326").to_crs(EPSG_MERC).iloc[0]
t_minx, t_miny, t_maxx, t_maxy = poly_merc.bounds
TARGET_NAME = os.path.splitext(os.path.basename(cfg.AREA_GEOJSON))[0]
print(f"目标范围: {area_path}")
print(f"EPSG:3857 边界框: ({t_minx:.0f}, {t_miny:.0f}) - ({t_maxx:.0f}, {t_maxy:.0f})")

# === 2. 扫描源目录：行号 -> 外层 zip 及其内层行 zip 条目 ===
root = cfg.SCLC_ROOT
if not os.path.isdir(root):
    print(f"错误: 土地覆盖源目录不存在: {root}")
    sys.exit(1)

row_entries = {}  # 行号(int) -> [(外层zip文件名, 内层条目名), ...]
for fname in sorted(os.listdir(root)):
    if not fname.lower().endswith(".zip"):
        continue
    with zipfile.ZipFile(os.path.join(root, fname)) as zf:
        for entry in zf.namelist():
            parts = entry.split("/")
            if len(parts) == 2 and parts[0].startswith("R") and parts[1].lower().endswith(".zip") \
                    and parts[0][1:].isdigit():
                row_entries.setdefault(int(parts[0][1:]), []).append((fname, entry))
if not row_entries:
    print("错误: 源目录中没有发现 R####/SCLC_R####_C*.zip 结构")
    sys.exit(1)
strips_sorted = sorted(row_entries)
print(f"源目录共 {len(strips_sorted)} 个条带（R{strips_sorted[0]:04d} - R{strips_sorted[-1]:04d}），"
      f"条带 = 固定 x 带约 19.5km 宽，瓦片 C### 为条带内沿 y 的序号")


# === 3. 抽取条带 zip 到缓存 + /vsizip 读瓦片 ===
cache_dir = os.path.join(script_dir, cfg.SCLC_CACHE_DIR)
os.makedirs(cache_dir, exist_ok=True)


def vsizip_uri(zip_path, member):
    return "/vsizip/" + os.path.abspath(zip_path).replace(os.sep, "/") + "/" + member


def strip_cache_path(strip, part):
    suffix = "" if part == 0 else f"_{part}"
    return os.path.join(cache_dir, f"R{strip:04d}{suffix}.zip")


_strip_info = {}


def strip_info(strip):
    """返回该条带 (x_left, x_right, [(条带zip路径, 瓦片名), ...])；条带不存在返回 None。

    条带内所有瓦片共享同一 x 带（实测），x 取第一块即可。
    """
    if strip in _strip_info:
        return _strip_info[strip]
    entries = row_entries.get(strip)
    if not entries:
        return None
    tiles = []
    for part, (outer_name, inner_entry) in enumerate(entries):
        zpath = strip_cache_path(strip, part)
        if not os.path.exists(zpath):
            outer = zipfile.ZipFile(os.path.join(root, outer_name))
            with open(zpath, "wb") as f:
                f.write(outer.read(inner_entry))
        with zipfile.ZipFile(zpath) as inner:
            for tif in inner.namelist():
                if tif.lower().endswith(".tif"):
                    tiles.append((zpath, tif))
    with rasterio.open(vsizip_uri(*tiles[0])) as ds:
        b = ds.bounds
    info = (b.left, b.right, tiles)
    _strip_info[strip] = info
    return info


# === 4. 定位覆盖条带（x 单调性未知，先采样验证，再二分查找） ===
print("\n定位覆盖条带...")

# 采样三个条带判断 R 编号与 x 的方向（实测为 R 越大越偏西）
i0, i1, i2 = 0, len(strips_sorted) // 2, len(strips_sorted) - 1
x0 = strip_info(strips_sorted[i0])[0]
x1 = strip_info(strips_sorted[i1])[0]
x2 = strip_info(strips_sorted[i2])[0]
if not ((x0 < x1 < x2) or (x0 > x1 > x2)):
    print("  [提示] 条带编号与 x 不单调，改为全量扫描条带 x ...")
    cover_strips = [s for s in strips_sorted
                    if strip_info(s)[0] < t_maxx and strip_info(s)[1] > t_minx]
else:
    direction = 1 if x1 > x0 else -1   # key = direction * x，使 key 随 R 递增

    def strip_key(s):
        return direction * strip_info(s)[0]

    # 二分找第一个 key > direction*t_maxx 的条带下标（其左侧的条带 x 才可能进入目标）
    lo, hi = 0, len(strips_sorted)
    while lo < hi:
        mid = (lo + hi) // 2
        if strip_key(strips_sorted[mid]) <= direction * t_maxx:
            lo = mid + 1
        else:
            hi = mid
    cover_strips = []
    if direction == 1:
        # x 随 R 递增（向东）：相交条带在 lo 之前（x 更小的一侧），向下走
        i, step = lo - 1, -1
    else:
        # x 随 R 递减（向西）：相交条带在 lo 及之后（x 更小的一侧），向上走
        i, step = lo, +1
    while 0 <= i < len(strips_sorted):
        xl, xr, _ = strip_info(strips_sorted[i])
        if not (xl < t_maxx and xr > t_minx):
            break   # 沿 x 减小方向越过目标西边界后即可停止
        cover_strips.append(strips_sorted[i])
        i += step
    cover_strips.sort()
if not cover_strips:
    print("错误: 未找到覆盖目标范围 x 的条带，请检查边界与数据覆盖范围（中国南方）")
    sys.exit(1)
for s in cover_strips:
    xl, xr, tiles = strip_info(s)
    print(f"  R{s:04d}: x {xl:,.0f} - {xr:,.0f}（{len(tiles)} 瓦）")


# === 5. 选瓦片（覆盖条带内按 y 相交筛选） ===
selected = []
for s in cover_strips:
    _, _, tiles = strip_info(s)
    for zpath, tif in tiles:
        with rasterio.open(vsizip_uri(zpath, tif)) as ds:
            b = ds.bounds
        if b.left < t_maxx and b.right > t_minx and b.bottom < t_maxy and b.top > t_miny:
            selected.append((zpath, tif))
if not selected:
    print("错误: 覆盖条带中没有与目标范围相交的瓦片")
    sys.exit(1)
print(f"\n选中 {len(selected)} 个瓦片:")
for zpath, tif in selected:
    print(f"  - {tif}")


# === 6. merge 拼接 + 掩膜裁剪 ===
print("\n拼接并裁剪...")
src_datasets = [rasterio.open(vsizip_uri(z, t)) for z, t in selected]

# 注意：不能用逐瓦片 reproject 到同一目标——reproject 默认 init_dest_nodata=True，
# 每次调用会先把目标整幅清成 nodata，后写的瓦片会抹掉先写的（实测踩坑）。
# merge 按窗口写入不清底，且各瓦片同为 1.5m 对齐网格，直接拼接。
mosaic, mosaic_transform = merge(src_datasets, nodata=cfg.SCLC_NODATA)
mosaic_profile = {
    "driver": "GTiff", "height": mosaic.shape[1], "width": mosaic.shape[2],
    "count": mosaic.shape[0], "dtype": "uint8",
    "crs": EPSG_MERC,          # 源瓦片 CRS 是 LOCAL_CS 声明，这里显式改为 EPSG:3857
    "transform": mosaic_transform, "nodata": cfg.SCLC_NODATA,
}

with MemoryFile() as memfile:
    with memfile.open(**mosaic_profile) as mosaic_ds:
        mosaic_ds.write(mosaic)
        geoms = [json.loads(gpd.GeoSeries([poly_merc], crs=EPSG_MERC).to_json())
                 ["features"][0]["geometry"]]
        out_image, out_transform = mask(mosaic_ds, geoms, crop=True,
                                        filled=True, nodata=cfg.SCLC_NODATA)

for ds in src_datasets:
    ds.close()

out_dir = cfg.SCLC_OUT_DIR if os.path.isabs(cfg.SCLC_OUT_DIR) \
    else os.path.join(script_dir, cfg.SCLC_OUT_DIR)
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, cfg.SCLC_OUT_FILENAME.format(TARGET_NAME=TARGET_NAME))
with rasterio.open(out_path, "w", driver="GTiff", height=out_image.shape[1],
                   width=out_image.shape[2], count=1, dtype="uint8",
                   crs=EPSG_MERC, transform=out_transform,
                   nodata=cfg.SCLC_NODATA, compress="lzw") as dst:
    write(dst, out_image)
    dst.write_colormap(1, SCLC_CMAP)

size_mb = os.path.getsize(out_path) / 1024 / 1024
print(f"土地覆盖已保存: {out_path} ({size_mb:.1f} MB)")

# === 同名 .qml 样式侧车：QGIS 加载 tif 时自动套用，颜色↔中文类别名直接映射 ===
# （Paletted 渲染 + paletteEntry label；tif 已在 QGIS 中打开过的，需移除图层
#   重新加载、或 属性→样式→加载样式 选该 qml 才会应用）
palette_xml = "\n".join(
    '        <paletteEntry value="{v}" color="#{c[0]:02x}{c[1]:02x}{c[2]:02x}" '
    'alpha="{c[3]}" label="{name}"/>'.format(
        v=v, c=SCLC_CMAP[v], name=SCLC_CLASS_NAMES[v])
    for v in sorted(SCLC_CMAP))
qml_path = os.path.splitext(out_path)[0] + ".qml"
with open(qml_path, "w", encoding="utf-8") as f:
    f.write(f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis' 'qgis.dtd'>
<qgis version="3.28" styleCategories="Symbology">
  <pipe>
    <rasterrenderer band="1" type="paletted" opacity="1" alphaBand="-1" nodata="">
      <colorPalette>
{palette_xml}
      </colorPalette>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0"/>
    <huesaturation colorizeOn="0" colorizeRed="255" colorizeGreen="128" colorizeBlue="128" colorizeStrength="100" grayscaleMode="0" saturation="0" saturationEnabled="0"/>
    <rasterresampler zoomedInResampler="nearestNeighbour" zoomedOutResampler="nearestNeighbour" maxOversampling="2" maxUndersampling="2" useXmlMaxResamplingLevel="1"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
""")
print(f"QML 样式已保存: {qml_path}（QGIS 打开 tif 自动按中文类别名着色显示图例）")

# 同目录写颜色-属性对应表说明（utf-8-sig 带 BOM，记事本直开不乱码）
legend_path = os.path.join(out_dir, "颜色-属性对应表.txt")
with open(legend_path, "w", encoding="utf-8-sig") as f:
    f.write(f"""{os.path.basename(out_path)} 颜色-属性对应表
====================================================
数据：2020 中国南方 1.5 米土地覆盖栅格（八种用地分类）
坐标系：EPSG:3857，像元 1.5m，uint8
说明：tif 已内嵌下表颜色的调色板，且同目录有同名 .qml 样式文件，
     QGIS 加载 tif 时自动套用 —— 颜色与类别名直接对应，图例显示中文；
     无数据值 {cfg.SCLC_NODATA} 渲染为透明。

像元值   类别                                  颜色名    RGB
--------------------------------------------------------------
 0       乔木/林地  Tree cover                 绿        (34, 139, 34)
 1       建成区     Build-up                   红        (255, 0, 0)
 2       灌草地     Shrubland and Grassland    黄绿      (189, 183, 107)
 3       冰雪       Snow and Ice               白        (240, 240, 255)
 4       耕地       Cropland                   黄        (255, 255, 0)
 5       水体       Water                      蓝        (0, 0, 255)
 6       裸地/稀疏植被  Bare or sparse veg.     棕        (197, 145, 90)
 7       其他       Others                     灰        (128, 128, 128)
{cfg.SCLC_NODATA:>2}       无数据     nodata                     透明      (0, 0, 0, 0)

QGIS 中手动改分类样式：图层属性 → 符号化 → 渲染类型选
「调色板/唯一值」(Paletted/Unique Values) → 点「分类」→ 可逐类改名、调色。
tif 之前已在 QGIS 中打开过时：移除图层重新加载，或
图层属性 → 样式 → 加载样式 → 选择同名 .qml。

分类编码精确含义以源目录 ClassificationSystem.doc 与论文
s41597-025-06373-y.pdf 为准。
生成工具：basic geoinfo fetch
""")
print(f"颜色-属性对应表已保存: {legend_path}")

# === 7. 结果统计 ===
vals, counts = np.unique(out_image, return_counts=True)
print(f"\n=== 土地覆盖统计 ===")
print(f"尺寸: {out_image.shape[2]} x {out_image.shape[1]} 像素")
print(f"分辨率: {out_transform.a:.3f} x {-out_transform.e:.3f} m")
print(f"像元分类分布（{cfg.SCLC_NODATA} 为无数据）:")
for v, c in zip(vals, counts):
    tag = "（无数据）" if v == cfg.SCLC_NODATA else ""
    print(f"  类别 {v:>3}: {c:>12,} 像元 ({c / out_image.size * 100:5.1f}%){tag}")
print("分类号含义见源目录 ClassificationSystem.doc / s41597-025-06373-y.pdf")

t_total = time.time() - t_start
print(f"\n=== 耗时统计 ===")
print(f"总耗时: {t_total:.1f} 秒 ({t_total/60:.1f} 分钟)")
print("\n全部完成!")
