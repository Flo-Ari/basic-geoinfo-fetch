# Provider defaults; paths and credentials are injected by the task runner.
AREA_BUFFER = 0.01
DEM_BUCKET = 'https://copernicus-dem-30m.s3.amazonaws.com'
DEM_TILE_PREFIX = 'Copernicus_DSM_COG_10'
DEM_OUT_FILENAME = '{TARGET_NAME}_DEM_30m.tif'
DEM_CACHE_DIR = 'cache_dem'
DEM_NODATA = -9999.0
PROXY = ''
SCLC_OUT_FILENAME = '{TARGET_NAME}_土地覆盖_1.5m.tif'
SCLC_CACHE_DIR = 'cache_landuse'
SCLC_NODATA = 25
SCLC_TARGET_EPSG = 3857
EULUC_ZIP_MEMBER = '按城市划分.zip/按城市划分/重庆市.shp'
EULUC_CACHE_DIR = 'cache_euluc'
EULUC_OUT_FILENAME = '{TARGET_NAME}_城市用地类型_2018.geojson'
