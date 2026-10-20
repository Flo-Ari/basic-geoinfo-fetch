from pathlib import Path
from fastapi.testclient import TestClient

def test_job_keeps_selected_directory_and_uses_internal_work_area(tmp_path,monkeypatch):
    from geofetch import jobs
    monkeypatch.setattr(jobs,'RUNTIME',tmp_path/'runtime')
    manager=jobs.JobManager()
    monkeypatch.setattr(manager.executor,'submit',lambda *args:None)
    output=tmp_path/'selected'
    try:
        request=dict(datasets=['dem'],output=str(output))
        job=manager.create(request)
        assert Path(job['output'])==output
        assert list(output.iterdir())==[]
        assert Path(request['output']).is_relative_to(tmp_path/'runtime'/job['id'])
        assert job['files']==[]
    finally:
        manager.executor.shutdown(wait=True)

def test_file_list_does_not_include_unrelated_files(tmp_path,monkeypatch):
    from geofetch import api
    (tmp_path/'owned.tif').write_bytes(b'own')
    (tmp_path/'unrelated.txt').write_text('private')
    monkeypatch.setitem(api.manager.jobs,'flat-files-test',dict(id='flat-files-test',output=str(tmp_path),files=['owned.tif']))
    client=TestClient(api.app)
    assert [item['name'] for item in client.get('/api/jobs/flat-files-test/files').json()]==['owned.tif']
    assert client.get('/api/jobs/flat-files-test/download/unrelated.txt').status_code==404
