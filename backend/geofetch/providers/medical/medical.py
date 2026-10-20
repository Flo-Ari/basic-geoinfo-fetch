"""OSM healthcare POIs, reusing the existing national PBF when available."""
import hashlib
import json
from pathlib import Path

import osmium
import requests
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import polygonize, unary_union

from ...progress import report, track

SCHEMA = 1
AMENITIES = {'hospital', 'clinic', 'doctors', 'dentist', 'pharmacy'}
LABELS = {'hospital': '医院', 'clinic': '诊所', 'doctors': '医生诊所',
          'dentist': '口腔医疗', 'pharmacy': '药房', 'centre': '医疗中心',
          'health_post': '卫生站', 'rehabilitation': '康复机构',
          'laboratory': '医学检验', 'blood_donation': '献血站', 'yes': '医疗设施'}
ENDPOINTS = ('https://overpass-api.de/api/interpreter',
             'https://overpass.kumi.systems/api/interpreter')


def category(tags):
    amenity = tags.get('amenity', '')
    if amenity in AMENITIES:
        return amenity
    healthcare = tags.get('healthcare', '')
    return ('doctors' if healthcare == 'doctor' else healthcare) if healthcare not in {'', 'no'} else ''


def read_area(path):
    obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    features = obj['features'] if obj['type'] == 'FeatureCollection' else [obj]
    polygons = []
    for feature in features:
        geom = shape(feature['geometry'] if feature['type'] == 'Feature' else feature)
        if geom.geom_type in {'LineString', 'MultiLineString'}:
            lines = [geom] if geom.geom_type == 'LineString' else list(geom.geoms)
            pieces = list(polygonize(lines))
            geom = unary_union(pieces) if pieces else geom.buffer(.01)
        elif geom.geom_type not in {'Polygon', 'MultiPolygon'}:
            geom = geom.buffer(.01)
        polygons.append(geom)
    area = unary_union(polygons)
    if area.is_empty or not area.is_valid:
        raise ValueError('范围为空或无效')
    return area


def way_geometry(points):
    if len(points) < 2:
        return None
    geom = Polygon(points) if len(points) >= 4 and points[0] == points[-1] else LineString(points)
    return geom if not geom.is_empty and geom.is_valid else None


def relation_geometry(members):
    outer, inner = [], []
    for role, points in members:
        if len(points) >= 2:
            (inner if role == 'inner' else outer).append(LineString(points))
    shells = list(polygonize(outer))
    if not shells:
        return None
    geom = unary_union(shells)
    holes = list(polygonize(inner))
    if holes:
        geom = geom.difference(unary_union(holes))
    return geom if not geom.is_empty and geom.is_valid else None


def extract_pbf(path):
    """Three native-filtered passes; store coordinates only for medical objects."""
    records, ways = [], {}
    report('提取医疗设施', 0, 3, '步骤', force=True)
    print(f'复用全国 OSM 原始数据：{path}', flush=True)
    processor = osmium.FileProcessor(str(path)).with_filter(osmium.filter.KeyFilter('amenity', 'healthcare'))
    for obj in processor:
        tags = dict(obj.tags)
        if not category(tags):
            continue
        record = dict(osm_type=obj.type_str(), osm_id=obj.id, tags=tags)
        if obj.is_node():
            if not obj.location.valid():
                continue
            record['geometry'] = mapping(Point(obj.location.lon, obj.location.lat))
        elif obj.is_way():
            ways[obj.id] = [node.ref for node in obj.nodes]
        elif obj.is_relation():
            if tags.get('type') != 'multipolygon':
                continue
            record['members'] = [(member.ref, member.role) for member in obj.members
                                 if member.type == 'w' and member.role in {'', 'outer', 'inner'}]
        records.append(record)
    print(f'发现全国医疗对象：{len(records):,} 个', flush=True)
    report('提取医疗设施', 1, 3, '步骤', force=True)
    missing = {ref for record in records for ref, _ in record.get('members', [])} - ways.keys()
    if missing:
        for way in osmium.FileProcessor(str(path), osmium.osm.WAY).with_filter(osmium.filter.IdFilter(missing)):
            ways[way.id] = [node.ref for node in way.nodes]
    report('提取医疗设施', 2, 3, '步骤', force=True)
    wanted = {ref for refs in ways.values() for ref in refs}
    coords = {}
    if wanted:
        for node in osmium.FileProcessor(str(path), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(wanted)):
            if node.location.valid():
                coords[node.id] = (node.location.lon, node.location.lat)
    def points(ref):
        refs = ways.get(ref, [])
        return [coords[node] for node in refs] if refs and all(node in coords for node in refs) else []
    features, skipped = [], 0
    for record in records:
        if record['osm_type'] == 'w':
            geom = way_geometry(points(record['osm_id']))
        elif record['osm_type'] == 'r':
            parts = [(role, points(ref)) for ref, role in record['members']]
            geom = relation_geometry(parts) if parts and all(part for _, part in parts) else None
        else:
            geom = shape(record['geometry'])
        if geom is None:
            skipped += 1
            continue
        features.append(dict(type='Feature', geometry=mapping(geom), properties={
            'osm_type': {'n': 'node', 'w': 'way', 'r': 'relation'}[record['osm_type']],
            'osm_id': record['osm_id'], 'tags': record['tags']}))
    report('提取医疗设施', 3, 3, '步骤', force=True)
    print(f'医疗几何有效：{len(features):,}；缺失引用或无效几何跳过：{skipped:,}', flush=True)
    return features


def fetch_overpass(area, cache):
    west, south, east, north = area.bounds
    bbox = f'{south},{west},{north},{east}'
    query = ('[out:json][timeout:120];('
             f'nwr["amenity"~"^(hospital|clinic|doctors|dentist|pharmacy)$"]({bbox});'
             f'nwr["healthcare"]["healthcare"!="no"]({bbox}););out geom;')
    path = cache / (hashlib.sha256(query.encode()).hexdigest()[:20] + '.overpass.json')
    if path.exists():
        obj = json.loads(path.read_text(encoding='utf-8'))
        print(f'[缓存命中] {path.name}', flush=True)
    else:
        errors = []
        for endpoint in ENDPOINTS:
            try:
                report('下载', force=True)
                print(f'按范围查询 OSM：{endpoint}', flush=True)
                with requests.post(endpoint, data={'data': query}, timeout=(20, 150),
                                   headers={'User-Agent': 'basic-geoinfo-fetch/1.0'}) as response:
                    response.raise_for_status()
                    obj = response.json()
                if obj.get('remark') or not isinstance(obj.get('elements'), list):
                    raise ValueError(f'OSM 查询未完整返回：{obj.get("remark", "缺少 elements")}')
                temp = path.with_suffix('.part')
                temp.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
                temp.replace(path)
                break
            except (requests.RequestException, ValueError) as exc:
                errors.append(f'{endpoint}: {exc}')
        else:
            raise RuntimeError('OSM 查询失败；可先运行道路采集以缓存全国 PBF。\n' + '\n'.join(errors))
    features = []
    for item in obj['elements']:
        tags = item.get('tags', {})
        if not category(tags):
            continue
        if item['type'] == 'node':
            geom = Point(item['lon'], item['lat'])
        elif item['type'] == 'way':
            points = item.get('geometry', [])
            geom = way_geometry([(p['lon'], p['lat']) for p in points]) if all(points) else None
        else:
            members = [m for m in item.get('members', []) if m.get('type') == 'way'
                       and m.get('role', '') in {'', 'outer', 'inner'}]
            if tags.get('type') != 'multipolygon' or not members or any(
                    not m.get('geometry') or any(p is None for p in m['geometry']) for m in members):
                continue
            geom = relation_geometry([(m.get('role', ''), [(p['lon'], p['lat']) for p in m['geometry']]) for m in members])
        if geom is not None:
            features.append(dict(type='Feature', geometry=mapping(geom), properties={
                'osm_type': item['type'], 'osm_id': item['id'], 'tags': tags}))
    report('下载', 1, 1, '查询', force=True)
    return features, 'OpenStreetMap / Overpass API'


def collect(area_path, output, cache, cache_root):
    area, output, cache = read_area(area_path), Path(output), Path(cache)
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    candidates = [p for folder in ('roads', 'hydro', 'medical')
                  for p in (Path(cache_root) / folder).glob('*.osm.pbf') if p.is_file()]
    if candidates:
        pbf = max(candidates, key=lambda p: p.stat().st_mtime_ns)
        stat = pbf.stat()
        signature = f'{SCHEMA}:{pbf.resolve()}:{stat.st_size}:{stat.st_mtime_ns}'
        cached = cache / (hashlib.sha256(signature.encode()).hexdigest()[:20] + '.medical.geojson')
        report('提取医疗设施', force=True)
        if cached.exists():
            features = json.loads(cached.read_text(encoding='utf-8'))['features']
            print(f'[缓存命中] {cached.name}', flush=True)
        else:
            features = extract_pbf(pbf)
            temp = cached.with_suffix('.part')
            temp.write_text(json.dumps(dict(type='FeatureCollection', features=features), ensure_ascii=False), encoding='utf-8')
            temp.replace(cached)
        report('提取医疗设施', 3, 3, '步骤', force=True)
        source = f'OpenStreetMap / {pbf.name}'
    else:
        features, source = fetch_overpass(area, cache)
    results, seen = [], set()
    for feature in track(features, len(features), '裁剪'):
        props, geom = feature['properties'], shape(feature['geometry'])
        key = (props['osm_type'], props['osm_id'])
        if key in seen or not geom.intersects(area):
            continue
        # Clipped representative points remain inside the selected area, including holes.
        point = geom.intersection(area).representative_point()
        tags = props['tags']
        kind = category(tags)
        attributes = dict(osm_type=key[0], osm_id=key[1],
                          osm_url=f'https://www.openstreetmap.org/{key[0]}/{key[1]}',
                          name=tags.get('name', tags.get('name:zh', '')), category=kind,
                          category_zh=LABELS.get(kind, '其他医疗设施'),
                          longitude=point.x, latitude=point.y,
                          poi_method='original_node' if geom.geom_type == 'Point' else 'clipped_representative_point',
                          source_geometry=geom.geom_type)
        for field in ('amenity', 'healthcare', 'healthcare:speciality', 'emergency', 'beds',
                      'operator', 'opening_hours'):
            attributes[field] = tags.get(field, '')
        attributes.update(phone=tags.get('contact:phone', tags.get('phone', '')),
                          website=tags.get('contact:website', tags.get('website', '')),
                          address=tags.get('addr:full') or ' '.join(tags.get('addr:' + k, '') for k in
                              ('province', 'city', 'district', 'street', 'housenumber')).strip())
        results.append(dict(type='Feature', geometry=mapping(point), properties=dict(attributes, osm_tags=tags)))
        seen.add(key)
    report('裁剪', len(features), len(features), '要素', force=True)
    report('写入成果', force=True)
    name = Path(area_path).stem + '_医疗设施POI'
    collection = dict(type='FeatureCollection', source=source, copyright='© OpenStreetMap contributors',
                      license='ODbL-1.0', license_url='https://www.openstreetmap.org/copyright', features=results)
    (output / (name + '.geojson')).write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding='utf-8')
    report('写入成果', 1, 1, '成果', force=True)
    print(f'范围内医疗 POI：{len(results):,} 个；坐标系：EPSG:4326', flush=True)
    if not results:
        print('当前范围在该 OSM 数据版本中未找到医疗设施，已输出空结果；不代表实际没有医院。', flush=True)
    return results
