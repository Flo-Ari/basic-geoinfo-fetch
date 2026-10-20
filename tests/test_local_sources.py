import io
import json
import struct
import sys
import zipfile

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

from geofetch import worker

@pytest.fixture
def setup(tmp_path,monkeypatch):
    area=tmp_path/'target.geojson'
    area.write_text(json.dumps(dict(type='Feature',properties={},geometry=mapping(box(104.05,31.05,104.06,31.06)))) ,encoding='utf-8')
    cache=tmp_path/'cache'
    cache.mkdir()
    monkeypatch.setattr(worker,'CACHE',cache)
    monkeypatch.setattr(sys,'argv',list(sys.argv))
    monkeypatch.setenv('GEOFETCH_WORK_DIR',str(cache))
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(worker.PROVIDERS/'landuse'))
    return tmp_path,area,cache

@pytest.mark.parametrize('legacy_utf8',[False,True])
def test_euluc_extracts_chinese_member_and_clips_geometry(setup,legacy_utf8):
    root,area,cache=setup
    city=root/'city'
    city.mkdir()
    shp=city/'重庆市.shp'
    gpd.GeoDataFrame({'Level1':[101]},geometry=[box(104.04,31.04,104.08,31.08)],crs='EPSG:4326').to_file(shp)
    source=root/'source'
    source.mkdir()
    archive=source/'按城市划分.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for part in city.iterdir():
            z.write(part,'按城市划分/'+part.name)
    if legacy_utf8:
        data=bytearray(archive.read_bytes())
        for marker,flag_offset in [(b'PK\x03\x04',6),(b'PK\x01\x02',8)]:
            offset=0
            while (offset:=data.find(marker,offset))>=0:
                flag=struct.unpack_from('<H',data,offset+flag_offset)[0]
                struct.pack_into('<H',data,offset+flag_offset,flag & ~0x800)
                offset+=4
        archive.write_bytes(data)
    worker.run('euluc',dict(area=str(area),output=str(root/'result'),euluc_source=str(source),euluc_member='按城市划分.zip/按城市划分/重庆市.shp'))
    files=list((root/'result'/'euluc').glob('*.geojson'))
    assert len(files)==1
    result=gpd.read_file(files[0])
    assert len(result)==1
    assert result.geometry.iloc[0].equals_exact(box(104.05,31.05,104.06,31.06),.00001,normalize=True)

def test_landcover_nested_archive_outputs_raster_and_style(setup):
    root,area,cache=setup
    source=root/'source'
    source.mkdir()
    extent=gpd.GeoSeries([box(104.04,31.04,104.07,31.07)],crs='EPSG:4326').to_crs(3857).total_bounds
    left,bottom,right,top=extent
    tile=root/'SCLC_R0001_C001.tif'
    with rasterio.open(tile,'w',driver='GTiff',width=100,height=100,count=1,dtype='uint8',nodata=25,crs='EPSG:3857',transform=from_origin(left,top,(right-left)/100,(top-bottom)/100)) as dst:
        dst.write(np.full((1,100,100),4,dtype='uint8'))
    inner=io.BytesIO()
    with zipfile.ZipFile(inner,'w') as z:
        z.write(tile,tile.name)
    with zipfile.ZipFile(source/'SCLC_R0001_R0001.zip','w') as outer:
        outer.writestr('R0001/SCLC_R0001_C1.zip',inner.getvalue())
    worker.run('landcover',dict(area=str(area),output=str(root/'result'),landcover_source=str(source)))
    folder=root/'result'/'landcover'
    files=list(folder.glob('*.tif'))
    assert len(files)==1 and list(folder.glob('*.qml'))
    with rasterio.open(files[0]) as src:
        assert src.crs.to_epsg()==3857
        assert float(src.read(1,masked=True).mean())==4
        assert src.width<100 and src.height<100
