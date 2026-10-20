# Provider defaults; paths and credentials are injected by the task runner.
AREA_PADDING = 0.02
AREA_BUFFER = 0.01
NATIONAL_PBF_TMPL = 'china-{time}.hydro.osm.pbf'
NATIONAL_GEOJSON_TMPL = 'china-{time}.hydro.osm.geojson'
PBF_MIRRORS = ['https://download.openstreetmap.fr/extracts/asia/china-latest.osm.pbf', 'https://download.geofabrik.de/asia/china-latest.osm.pbf']
OUT_DIR = 'results'
OUT_FILENAME = '{TARGET_NAME}_Hydro.geojson'
WATERWAY_LINE_VALUES = {'river', 'stream', 'canal', 'ditch', 'drain', 'tidal_channel'}
WATER_AREA_RULES = [('natural', 'water'), ('waterway', 'riverbank'), ('landuse', 'reservoir'), ('landuse', 'basin'), ('landuse', 'salt_pond')]
KEEP_KEYS = {'waterway', 'natural', 'water', 'landuse', 'name', 'ref', 'int_ref', 'width', 'intermittent', 'seasonal', 'tidal', 'salt'}
PROXY = ''
