import json
from pathlib import Path
from geofetch.artifacts import publish

def test_products_are_flat_and_existing_files_are_not_overwritten(tmp_path):
    source, output = tmp_path/'internal'/'dem', tmp_path/'chosen'
    source.mkdir(parents=True)
    output.mkdir()
    (output/'area.tif').write_bytes(b'previous')
    (source/'area.tif').write_bytes(b'new')
    (source/'area.qml').write_bytes(b'style')
    files=publish(source,output,'dem')
    assert set(files)=={'area_2.tif','area_2.qml'}
    assert (output/'area.tif').read_bytes()==b'previous'
    assert (output/'area_2.tif').read_bytes()==b'new'
    assert not any(path.is_dir() for path in output.iterdir())

def test_event_pdfs_are_flat_and_manifest_points_to_new_names(tmp_path):
    source, output = tmp_path/'internal'/'events', tmp_path/'chosen'
    nested=source/'EMSR001'/'AOI01'/'product'
    nested.mkdir(parents=True)
    (nested/'EMSR001_map.pdf').write_bytes(b'pdf')
    (nested/'_download.json').write_text('{}')
    (source/'manifest.json').write_text(json.dumps({'files':['EMSR001/AOI01/product/EMSR001_map.pdf']}))
    files=publish(source,output,'events')
    assert set(files)=={'EMSR001_map.pdf','manifest.json'}
    assert json.loads((output/'manifest.json').read_text())['files']==['EMSR001_map.pdf']
    assert not any(path.is_dir() for path in output.iterdir())
