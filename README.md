# basic geoinfo fetch｜基础地理信息数据获取

本地运行的地理数据获取工具，支持范围裁剪、多数据采集、缓存复用和结果导出。

## 支持的数据

| 数据 | 来源 | 输出 |
| --- | --- | --- |
| 数字高程 | [Copernicus DEM GLO-30](https://documentation.dataspace.copernicus.eu/Data/Others/CCM.html)，约 30m DSM | GeoTIFF |
| 人口分布 | [WorldPop](https://hub.worldpop.org/)，R2025A，中国 2026 年 100m 人口数栅格 | GeoTIFF |
| 道路与铁路 | [OpenStreetMap](https://www.openstreetmap.org/)，中国区域数据 | GeoJSON |
| 河流与水体 | OpenStreetMap，中国区域数据 | GeoJSON |
| 土地覆盖 | 自定义数据源 | GeoTIFF、QGIS 样式 |
| 城市用地 | 自定义数据源 | GeoJSON |
| 灾害事件地图 | [Copernicus EMS Rapid Mapping](https://mapping.emergency.copernicus.eu/) | PDF |

土地覆盖和城市用地需要提供本地源数据。灾害地图按事件下载，不按目标范围裁剪。

## 安装与使用

环境要求：Python 3.11+（推荐 3.12）、Node.js 22.12+ 或 20.19+。首次安装和在线下载需要联网。

**Windows**

1. 双击 `setup.cmd` 安装依赖。
2. 双击 `start.cmd`，打开 [本地工作台](http://127.0.0.1:8765)。
3. 选择数据类型、上传或选择范围、指定保存目录，点击“开始采集”。

保持启动窗口运行，按 `Ctrl+C` 关闭服务。

**macOS / Linux**

```bash
python3 scripts/setup.py
.venv/bin/python run.py
```

## 范围与数据源

范围文件使用 **WGS84 / EPSG:4326 GeoJSON**，建议使用面要素，上传大小不超过 10 MB。

```text
backend/               # Python 后端
frontend/              # React 界面
sources/
  boundaries/          # 目标范围 GeoJSON
  landcover/           # 土地覆盖原始 ZIP 分卷
  euluc/               # 城市用地 ZIP
cache/                 # 下载及处理中间缓存
data/                  # 默认成果目录
```

选择土地覆盖或城市用地后，在“数据源设置”中填写源目录，也可以直接选用其他磁盘上的数据。

- **土地覆盖**：需要完整的两层 ZIP 结构：`外层.zip → R####/SCLC_R####_C*.zip → SCLC_R####_C###.tif`。
- **城市用地**：指定 ZIP 内部路径，例如 `按城市划分.zip/按城市划分/城市名.shp`；同名 `.shp/.shx/.dbf` 文件须齐全，建议包含 `.prj/.cpg`。

## 数据许可与致谢

感谢数据贡献者和研究团队。本工具不拥有上游数据版权，也不代表数据提供方的官方支持。使用或发布成果时，应遵守对应数据的许可并保留来源署名。

- **OpenStreetMap**：采用 [ODbL 1.0](https://www.openstreetmap.org/copyright)。道路、铁路和水系成果须署名 `© OpenStreetMap contributors`；公开衍生数据库时遵守适用的共享要求。
- **Copernicus DEM**：遵循 [GLO-30 专用许可](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/DEM/resources/license/License-COPDEM-30.pdf)，按第 6 条保留 DLR、Airbus、欧盟和 ESA 等来源声明及免责声明。
- **WorldPop**：按[官方声明](https://hub.worldpop.org/)的 **CC BY 4.0** 要求署名，注明数据版本、原始引用及裁剪等修改。
- **自定义数据源**：土地覆盖和城市用地的版权及使用范围由数据提供者决定，使用和分发时遵守对应数据包的许可。
- **Copernicus EMS**：遵循[官方条款](https://mapping.emergency.copernicus.eu/terms-and-conditions/)，保留地图的来源、版权、激活编号及第三方影像声明。

数据许可与软件代码许可分别适用。缓存、私人源数据、成果和本地配置默认不提交 Git。
