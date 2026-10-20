import json
import os
from pathlib import Path
import sys

import numpy as np
import rasterio
from rasterio.transform import from_origin

from geofetch import worker

def test_dem_reuses_cache_and_produces_georeferenced_crop(tmp_path, monkeypatch):
    cache = tmp_path/'cache'
    (cache/'dem').mkdir(parents=True)
    tile = cache/'dem'/'Copernicus_DSM_COG_10_N31_00_E104_00_DEM.tif'
    with rasterio.open(tile,'w',driver='GTiff',height=100,width=100,count=1,dtype='float32',crs='EPSG:4326',transform=from_origin(104,32,.01,.01),nodata=-9999) as dst:
        dst.write(np.full((1,100,100),125.,dtype='float32'))
    area = tmp_path/'target.geojson'
    area.write_text(json.dumps({'type':'Polygon','coordinates':[[[104.05,31.05],[104.15,31.05],[104.15,31.15],[104.05,31.15],[104.05,31.05]]]}),encoding='utf-8')
    import requests
    def no_network(*args, **kwargs):
        raise AssertionError('A completed cache must avoid network requests')
    monkeypatch.setattr(requests, 'get', no_network)
    monkeypatch.setattr(requests, 'head', no_network)
    monkeypatch.setattr(worker,'CACHE',cache)
    monkeypatch.setattr(sys,'argv',list(sys.argv))
    monkeypatch.setenv('GEOFETCH_WORK_DIR',str(cache))
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(worker.PROVIDERS/'dem'))
    worker.run('dem',dict(area=str(area),output=str(tmp_path/'result')))
    products=list((tmp_path/'result'/'dem').glob('*.tif'))
    assert len(products)==1
    with rasterio.open(products[0]) as src:
        assert src.crs.to_epsg()==4326
        assert src.width < 100 and src.height < 100
        assert float(src.read(1,masked=True).mean())==125.
    assert tile.is_file()

def test_restarted_jobs_are_interrupted_and_queued_cancel_is_final(tmp_path,monkeypatch):
    from geofetch import jobs
    monkeypatch.setattr(jobs,'RUNTIME',tmp_path)
    folder=tmp_path/'previous'
    folder.mkdir()
    (folder/'job.json').write_text(json.dumps(dict(id='previous',status='running')),encoding='utf-8')
    manager=jobs.JobManager()
    assert manager.jobs['previous']['status']=='interrupted'
    manager.jobs['queued']=dict(id='queued',status='queued')
    assert manager.cancel('queued')['status']=='cancelled'
    manager.executor.shutdown(wait=True)
