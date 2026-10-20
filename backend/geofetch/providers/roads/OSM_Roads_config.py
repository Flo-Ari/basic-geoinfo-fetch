# Provider defaults; paths and credentials are injected by the task runner.
AREA_PADDING = 0.02
AREA_BUFFER = 0.01
NATIONAL_PBF_TMPL = 'china-{time}.road.osm.pbf'
NATIONAL_GEOJSON_TMPL = 'china-{time}.road.osm.geojson'
PBF_MIRRORS = ['https://download.openstreetmap.fr/extracts/asia/china-latest.osm.pbf', 'https://download.geofabrik.de/asia/china-latest.osm.pbf']
OUT_DIR = 'results'
OUT_FILENAME = '{TARGET_NAME}_Roads_Railways.geojson'
HIGHWAY_ZH = {'motorway': '高速路', 'motorway_link': '高速路', 'trunk': '公路', 'trunk_link': '公路', 'primary': '公路', 'primary_link': '公路', 'secondary': '公路', 'secondary_link': '公路', 'tertiary': '公路', 'tertiary_link': '公路', 'unclassified': '公路', 'residential': '公路', 'living_street': '公路', 'service': '公路', 'track': '公路', 'road': '公路', 'busway': '公路', 'services': '公路', 'raceway': '公路', 'rest_area': '公路', 'footway': '步道', 'path': '步道', 'steps': '步道', 'pedestrian': '步道', 'bridleway': '步道', 'cycleway': '步道', 'corridor': '步道', 'elevator': '步道', 'construction': '规划路', 'proposed': '规划路', 'planned': '规划路'}
HIGHWAY_DEFAULT = '公路'
RAILWAY_ZH = {'rail': '铁路', 'subway': '地铁', 'tram': '铁路', 'light_rail': '铁路', 'monorail': '铁路', 'narrow_gauge': '铁路', 'funicular': '铁路', 'preserved': '铁路', 'miniature': '铁路', 'disused': '铁路', 'construction': '规划路', 'proposed': '规划路'}
WAY_DROP = {'abandoned', 'platform', 'station', 'turntable', 'traverser'}
KEEP_KEYS = {'highway', 'railway', 'name', 'ref', 'int_ref', 'gauge', 'electrified', 'voltage', 'frequency', 'usage', 'service'}
PROXY = ''
