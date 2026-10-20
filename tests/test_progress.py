import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.mask import mask as reference_mask
from rasterio.transform import from_origin
from shapely.geometry import Polygon, mapping

from geofetch import processing, progress

@pytest.mark.parametrize('filled', [True, False])
def test_windowed_crop_matches_rasterio_and_reports_percent(tmp_path, monkeypatch, filled):
    source = tmp_path/'source.tif'
    data = np.arange(2*120*130,dtype='int32').reshape(2,120,130)
    data[:,25:30,40:50] = -9999
    shape = mapping(Polygon([(1,11),(10,11),(10,2),(3,3),(1,11)]))
    with rasterio.open(source,'w',driver='GTiff',height=120,width=130,count=2,dtype='int32',crs=4326,transform=from_origin(0,12,.1,.1),nodata=-9999) as dst:
        dst.write(data)
    updates=[]
    monkeypatch.setattr(processing,'report',lambda *args,**kwargs:updates.append(args))
    with rasterio.open(source) as src:
        expected, expected_transform=reference_mask(src,[shape],crop=True,filled=filled,nodata=-9999)
        actual, actual_transform=processing.mask(src,[shape],filled=filled,nodata=-9999,block_size=16)
    np.testing.assert_array_equal(actual,expected)
    if not filled:
        np.testing.assert_array_equal(actual.mask,expected.mask)
    assert actual_transform==expected_transform
    crop_updates=[item for item in updates if item[0]=='裁剪']
    counts=[item[1] for item in crop_updates]
    assert len(counts)>10 and counts[0]==0 and counts[-1]==crop_updates[-1][2]

def test_structured_progress_is_real_and_unknown_totals_stay_unknown(capsys):
    progress.report('裁剪',3,10,'要素',force=True)
    output=capsys.readouterr().out
    values=[progress.parse(line) for line in output.splitlines() if line.startswith(progress.PREFIX)]
    assert values[0]['percent']==30
    assert '[裁剪] 30.0%' in output
    progress.report('转换 PBF',force=True)
    value=next(progress.parse(line) for line in capsys.readouterr().out.splitlines() if line.startswith(progress.PREFIX))
    assert value['percent'] is None
    assert progress.parse(progress.PREFIX+'{"stage":"test","percent":150}') is None
    assert progress.parse(progress.PREFIX+'bad') is None

def test_windowed_output_writes_all_bands_and_reports_actual_windows(tmp_path,monkeypatch):
    path=tmp_path/'written.tif'
    data=np.arange(2*40*50,dtype='int16').reshape(2,40,50)
    updates=[]
    monkeypatch.setattr(processing,'report',lambda *args,**kwargs:updates.append(args))
    with rasterio.open(path,'w',driver='GTiff',height=40,width=50,count=2,dtype='int16',crs=4326,transform=from_origin(104,32,.01,.01)) as dst:
        processing.write(dst,data,block_size=16)
    with rasterio.open(path) as src:
        np.testing.assert_array_equal(src.read(),data)
    assert updates[0][1]==0 and updates[-1][1]==updates[-1][2]==12

def test_supervisor_exposes_progress_before_process_exit(tmp_path, monkeypatch):
    from geofetch import jobs
    monkeypatch.setattr(jobs,'RUNTIME',tmp_path/'runtime')
    manager=jobs.JobManager()
    original_popen=subprocess.Popen
    code="import time; print('"+progress.PREFIX+json.dumps(dict(stage='裁剪',percent=37.5,completed=3,total=8))+"',flush=True); print('[裁剪] 37.5%',flush=True); time.sleep(1.3)"
    monkeypatch.setattr(jobs.subprocess,'Popen',lambda args,**kwargs:original_popen([sys.executable,'-u','-c',code],**kwargs))
    try:
        job=manager.create(dict(datasets=['dem'],output=str(tmp_path/'products')))
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            with manager.lock:
                current=dict(manager.jobs[job['id']])
            if current.get('progress',{}).get('percent')==37.5:
                break
            time.sleep(.03)
        assert current['status']=='running'
        assert current['progress']['percent']==37.5
        assert current['progress']['stage']=='裁剪'
        assert current['progress']['overall_percent']==77.5
    finally:
        manager.executor.shutdown(wait=True)
    final=manager.jobs[job['id']]
    assert final['status']=='completed' and final['progress']['percent']==100
    log=(tmp_path/'runtime'/job['id']/'log.txt').read_text(encoding='utf-8')
    assert '[裁剪] 37.5%' in log and progress.PREFIX not in log

def test_only_seven_public_datasets_remain():
    from geofetch.settings import CATALOG
    from geofetch.worker import ADAPTERS
    expected={'dem','population','roads','hydro','landcover','euluc','events'}
    assert {item['id'] for item in CATALOG}==expected
    assert set(ADAPTERS)==expected-{'events'}
