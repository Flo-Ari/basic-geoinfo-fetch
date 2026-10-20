import json
from pathlib import Path

import pytest
from shapely.geometry import Point, box, mapping, shape

from geofetch.providers.medical.medical import collect, extract_pbf, read_area, fetch_overpass
from geofetch.overall import DatasetProgress


OSM = '''<osm version="0.6">
<node id="1" lat="1" lon="1"><tag k="amenity" v="hospital"/><tag k="name" v="医院甲"/></node>
<node id="2" lat="9" lon="9"><tag k="healthcare" v="clinic"/></node>
<node id="3" lat="0" lon="0"/>
<node id="4" lat="0" lon="4"/>
<node id="5" lat="4" lon="4"/>
<node id="6" lat="4" lon="0"/>
<node id="7" lat="1.5" lon="1.5"/>
<node id="8" lat="1.5" lon="2.5"/>
<node id="9" lat="2.5" lon="2.5"/>
<node id="10" lat="2.5" lon="1.5"/>
<node id="11" lat="1" lon="8"/>
<node id="12" lat="2" lon="8"/>
<node id="13" lat="2" lon="9"/>
<node id="14" lat="1" lon="9"/>
<way id="20"><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="6"/><nd ref="3"/></way>
<way id="21"><nd ref="7"/><nd ref="8"/><nd ref="9"/><nd ref="10"/><nd ref="7"/></way>
<way id="22"><nd ref="11"/><nd ref="12"/><nd ref="13"/><nd ref="14"/><nd ref="11"/>
<tag k="amenity" v="hospital"/><tag k="name" v="医院乙"/></way>
<way id="23"><nd ref="11"/><nd ref="999"/><tag k="amenity" v="clinic"/></way>
<relation id="30"><member type="way" ref="20" role="outer"/><member type="way" ref="21" role="inner"/>
<tag k="type" v="multipolygon"/><tag k="healthcare" v="hospital"/><tag k="name" v="院区丙"/></relation>
</osm>'''


def test_extract_osm_nodes_ways_relations_and_missing_references(tmp_path):
    path = tmp_path / 'test.osm'
    path.write_text(OSM, encoding='utf-8')
    features = extract_pbf(path)
    assert {f['properties']['osm_id'] for f in features} == {1, 2, 22, 30}
    relation = next(f for f in features if f['properties']['osm_id'] == 30)
    geom = shape(relation['geometry'])
    assert geom.area == 15
    assert not geom.covers(Point(2, 2))


def test_collect_reuses_pbf_cache_and_outputs_only_in_range(tmp_path, monkeypatch):
    import osmium
    from geofetch.providers.medical import medical
    source = tmp_path / 'test.osm'
    source.write_text(OSM, encoding='utf-8')
    root = tmp_path / 'cache'
    (root / 'roads').mkdir(parents=True)
    pbf = root / 'roads' / 'test.osm.pbf'
    with osmium.SimpleWriter(str(pbf)) as writer:
        for item in osmium.FileProcessor(str(source)):
            writer.add(item)
    area_path = tmp_path / 'area.geojson'
    area = box(.5, .5, 8.5, 3)
    area_path.write_text(json.dumps(mapping(area)), encoding='utf-8')
    result = collect(area_path, tmp_path / 'output', root / 'medical', root)
    assert {f['properties']['osm_id'] for f in result} == {1, 22, 30}
    assert all(area.covers(shape(f['geometry'])) for f in result)
    assert all(f['geometry']['type'] == 'Point' for f in result)
    assert [p.suffix for p in (tmp_path / 'output').iterdir()] == ['.geojson']
    attributes = result[0]['properties']
    assert attributes['name'] == '医院甲'
    assert {'name', 'category', 'address', 'phone', 'longitude', 'latitude', 'osm_id', 'osm_tags'} <= attributes.keys()
    assert len(list((root / 'medical').glob('*.geojson'))) == 1
    monkeypatch.setattr(medical, 'extract_pbf', lambda _: pytest.fail('缓存未复用'))
    assert len(collect(area_path, tmp_path / 'output2', root / 'medical', root)) == 3


def test_closed_line_area_is_polygonized(tmp_path):
    path = tmp_path / 'area.geojson'
    path.write_text(json.dumps(mapping(box(0, 0, 1, 1).boundary)), encoding='utf-8')
    assert read_area(path).area == 1


def test_overpass_failed_partial_result_not_cached(tmp_path, monkeypatch):
    from geofetch.providers.medical import medical
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def json(self): return {'elements': [], 'remark': 'runtime error: timeout'}
    monkeypatch.setattr(medical.requests, 'post', lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match='OSM 查询失败'):
        fetch_overpass(box(0, 0, 1, 1), tmp_path)
    assert not list(tmp_path.glob('*.json'))


def test_overpass_fetches_geometry_and_reuses_query_cache(tmp_path, monkeypatch):
    from geofetch.providers.medical import medical
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def json(self):
            return {'elements': [
                {'type': 'node', 'id': 1, 'lat': 1, 'lon': 1, 'tags': {'amenity': 'hospital'}},
                {'type': 'way', 'id': 2, 'tags': {'healthcare': 'clinic'}, 'geometry': [
                    {'lon': 0, 'lat': 0}, {'lon': 2, 'lat': 0}, {'lon': 2, 'lat': 2},
                    {'lon': 0, 'lat': 2}, {'lon': 0, 'lat': 0}]},
                {'type': 'way', 'id': 3, 'tags': {'healthcare': 'clinic'}, 'geometry': [
                    {'lon': 0, 'lat': 0}, None, {'lon': 2, 'lat': 2}]}]}
    def post(*args, **kwargs):
        calls.append(kwargs['data']['data'])
        return Response()
    monkeypatch.setattr(medical.requests, 'post', post)
    features, source = fetch_overpass(box(0, 0, 3, 3), tmp_path)
    assert {f['properties']['osm_id'] for f in features} == {1, 2}
    assert len(fetch_overpass(box(0, 0, 3, 3), tmp_path)[0]) == 2
    assert len(calls) == 1
    assert 'out geom' in calls[0]


def test_medical_overall_progress_is_monotonic():
    progress = DatasetProgress('medical')
    values = [progress.update({'stage': s, 'percent': p})['overall_percent']
              for s, p in [('准备数据', 0), ('提取医疗设施', 100), ('裁剪', 50), ('写入成果', 100)]]
    assert values == sorted(values)
    assert 0 < values[1] < values[2] < values[3] < 100
    assert progress.finish()['overall_percent'] == 100
