import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from geofetch import api

AREA = {'type':'FeatureCollection','features':[{'type':'Feature','properties':{},'geometry':{'type':'Polygon','coordinates':[[[104.05,31.05],[104.06,31.05],[104.06,31.06],[104.05,31.06],[104.05,31.05]]]}}]}

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'SOURCES', tmp_path / 'sources')
    monkeypatch.setattr(api, 'RUNTIME', tmp_path / '.runtime')
    (api.SOURCES / 'boundaries').mkdir(parents=True)
    return TestClient(api.app)

def upload(client, obj=AREA):
    return client.post('/api/area', files={'file':('area.geojson',json.dumps(obj).encode(),'application/geo+json')})

def test_upload_validates_geometry_and_projection(client):
    valid = upload(client)
    assert valid.status_code == 200
    assert Path(valid.json()['path']).is_file()
    invalid = upload(client, {'type':'FeatureCollection','features':[]})
    assert invalid.status_code == 400
    projected = dict(AREA, crs={'type':'name','properties':{'name':'EPSG:3857'}})
    assert upload(client, projected).status_code == 400
    assert len(list(api.SOURCES.rglob('*.geojson'))) == 1

def test_cross_origin_and_unknown_dataset_rejected(client):
    assert client.post('/api/jobs', json={'datasets':['events']}, headers={'Origin':'https://example.com'}).status_code == 403
    assert client.post('/api/jobs', json={'datasets':['unknown']}).status_code == 400
    assert client.post('/api/jobs', json={'datasets':['dem']}).status_code == 400

def test_create_snapshots_area_and_resolves_output(client, tmp_path, monkeypatch):
    path = upload(client).json()['path']
    monkeypatch.setattr(api.manager, 'create', lambda payload: payload)
    response = client.post('/api/jobs', json={'datasets':['dem','dem'], 'area':path, 'output':str(tmp_path/'result')})
    assert response.status_code == 201
    result = response.json()
    assert result['datasets'] == ['dem']
    assert Path(result['area']) != Path(path)
    assert Path(result['area']).read_bytes() == Path(path).read_bytes()
    assert Path(result['area']).name == 'area.geojson' or Path(result['area']).name.endswith('_area.geojson')
    Path(path).write_text('{}', encoding='utf-8')
    assert json.loads(Path(result['area']).read_text(encoding='utf-8')) == AREA

def test_output_cannot_overwrite_source_tree(client):
    response = client.post('/api/jobs', json={'datasets':['events'], 'output':str(api.ROOT/'backend'/'products')})
    assert response.status_code == 400

def test_download_cannot_escape_job_directory(client, tmp_path, monkeypatch):
    output = tmp_path/'output'
    output.mkdir()
    (output/'ok.txt').write_text('ok')
    outside = tmp_path/'private.txt'
    outside.write_text('private')
    monkeypatch.setitem(api.manager.jobs, 'test-safe-download', {'id':'test-safe-download','output':str(output)})
    assert client.get('/api/jobs/test-safe-download/download/ok.txt').text == 'ok'
    assert client.get('/api/jobs/test-safe-download/download/%2e%2e%2fprivate.txt').status_code == 404

def test_sources_and_event_codes_checked_before_launch(client, tmp_path):
    area = upload(client).json()['path']
    assert client.post('/api/jobs', json={'datasets':['landcover'],'area':area,'landcover_source':str(tmp_path/'missing')}).status_code == 400
    assert client.post('/api/jobs', json={'datasets':['events'],'event_codes':['bad-code']}).status_code == 400


def test_explicit_empty_output_does_not_fall_back_to_data(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api.manager, 'create', lambda _: pytest.fail('不应启动任务'))
    response = client.post('/api/jobs', json={'datasets':['events'], 'output':'   '})
    assert response.status_code == 400
    assert '保存目录' in response.json()['detail']
