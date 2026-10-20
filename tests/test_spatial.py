import json
from pathlib import Path
from geofetch import spatial

def feature(name,coords):
    return dict(type='Feature',properties=dict(name=name),geometry=dict(type='LineString',coordinates=coords))

def write(path,features):
    path.write_text('{"type":"FeatureCollection","features":[\n'+',\n'.join(json.dumps(f) for f in features)+'\n]}\n',encoding='utf-8')

def test_index_selects_intersections_and_reuses_cache(tmp_path,monkeypatch):
    path=tmp_path/'china.geojson'
    write(path,[feature('in',[[104,31],[105,32]]),feature('out',[[110,35],[111,36]])])
    found=list(spatial.indexed_features(path,(104.2,31.2,104.5,31.5)))
    assert [f['properties']['name'] for f in found]==['in']
    monkeypatch.setattr(spatial,'build_index',lambda *args: (_ for _ in ()).throw(AssertionError('Index must be reused')))
    assert list(spatial.indexed_features(path,(120,40,121,41)))==[]

def test_changed_source_invalidates_index_and_recovers_partial(tmp_path):
    path=tmp_path/'china.geojson'
    write(path,[feature('before',[[104,31],[105,32]])])
    assert len(list(spatial.indexed_features(path,(100,20,120,40))))==1
    write(path,[feature('after',[[104,31],[105,32]]),feature('added',[[106,31],[107,32]])])
    index=path.with_suffix('.geojson.spatial.sqlite')
    index.with_name(index.name+'.part').write_bytes(b'incomplete')
    assert len(list(spatial.indexed_features(path,(100,20,120,40))))==2
