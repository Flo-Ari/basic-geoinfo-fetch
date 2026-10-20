import pytest
from geofetch.overall import DatasetProgress, PLANS

def event(stage, percent=None, **values):
    return dict(stage=stage, percent=percent, **values)

def test_download_and_merge_completion_are_not_dataset_completion():
    progress=DatasetProgress('dem')
    assert progress.update(event('下载',100))['overall_percent']==45
    assert progress.update(event('拼接栅格'))['overall_percent']==45
    assert progress.update(event('拼接栅格',100))['overall_percent']==65
    assert progress.update(event('准备裁剪'))['overall_percent']==65
    assert progress.update(event('裁剪',50))['overall_percent']==80
    assert progress.update(event('写入成果',100))['overall_percent']==99
    assert progress.finish()['overall_percent']==100

@pytest.mark.parametrize('dataset',PLANS)
def test_progress_never_rewinds_and_never_reaches_100_while_running(dataset):
    progress=DatasetProgress(dataset)
    last=0
    for stage in PLANS[dataset]:
        for percent in [None,0,50,100,0,100]:
            state=progress.update(event(stage,percent))
            assert last<=state['overall_percent']<=99
            assert state['overall_estimated'] is True
            assert state['percent']==percent
            last=state['overall_percent']
    assert progress.finish()['overall_estimated'] is False

def test_each_dataset_starts_at_its_own_zero():
    assert DatasetProgress('dem').finish()['overall_percent']==100
    assert DatasetProgress('population').update(event('准备数据'))['overall_percent']==0

def test_events_advance_by_event_count_and_keep_remaining_events():
    progress=DatasetProgress('events')
    progress.update(event('提取事件',0,completed=0,total=3))
    assert progress.update(event('下载',100))['overall_percent']<33
    assert progress.update(event('提取事件',100/3,completed=1,total=3))['overall_percent']==33
    assert progress.update(event('下载',100))['overall_percent']>33
    assert progress.update(event('提取事件',100,completed=3,total=3))['overall_percent']==99
