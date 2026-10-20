# Provider defaults; paths and credentials are injected by the task runner.
AREA_BUFFER = 0.01
POP_URL = 'ftp://ftp.worldpop.org/GIS/Population/Global_2015_2030/R2025A/2026/CHN/v1/100m/constrained/chn_pop_2026_CN_100m_R2025A_v1.tif'
POP_URL_HTTP = 'https://data.worldpop.org/GIS/Population/Global_2015_2030/R2025A/2026/CHN/v1/100m/constrained/chn_pop_2026_CN_100m_R2025A_v1.tif'
OUT_DIR = 'results'
OUT_FILENAME = '{TARGET_NAME}_Population_100m.tif'
CACHE_DIR = 'metaDEM'
NODATA = -9999.0
