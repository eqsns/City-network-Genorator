import json
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
import pytest
from app import JobStore, validate, make_server


@pytest.mark.parametrize('bbox', [None,[],[1,2,3],[None,0,1,1],['',0,1,1],[True,0,1,1],['x',0,1,1],[float('nan'),0,1,1],[float('inf'),0,1,1],[1,1,1,2],[2,1,1,2],[0,2,1,1],[-181,0,-180,1],[0,85,1,86],[0,0,10,10]])
def test_bad_ranges(bbox):
    with pytest.raises(ValueError):
        validate({'name':'测试','bbox':bbox})


def test_bbox_order_and_types():
    assert validate({'name':'测试','bbox':['118.77','32.02','118.78','32.03']})[1]==[118.77,32.02,118.78,32.03]


@pytest.mark.parametrize('area_km2', [500.01, 750, 1000])
def test_expanded_area_limit_accepts_up_to_1000(area_km2):
    # Symmetric latitude bounds make this rectangle's mean-latitude cosine 1.
    bbox = [0, -0.05, area_km2 / (111.32 * 111.32 * 0.1), 0.05]
    assert validate({'name': '扩大范围', 'bbox': bbox})[1] == bbox


@pytest.mark.parametrize('area_km2', [1000.01, 1500])
def test_expanded_area_limit_rejects_above_1000(area_km2):
    bbox = [0, -0.05, area_km2 / (111.32 * 111.32 * 0.1), 0.05]
    with pytest.raises(ValueError, match='1000 平方公里'):
        validate({'name': '过大范围', 'bbox': bbox})


def test_progress_monotonic_and_job_isolation(tmp_path):
    store=JobStore(tmp_path)
    job=dict(id='test',status='running',progress=25,stage='旧阶段')
    store.consume(job,dict(job_id='other',type='progress',percent=99,phase='x',message='错误'))
    assert job['progress']==25
    store.consume(job,dict(job_id='test',type='progress',percent=10,phase='x',message='新阶段'))
    assert job['progress']==25
    store.consume(job,dict(job_id='test',type='progress',percent=100,phase='x',message='写出'))
    assert job['progress']==99
    job['status']='cancelling'
    store.consume(job,dict(job_id='test',type='result',stats={'complete':True}))
    assert 'stats' not in job


def test_interrupted_and_relative_paths(tmp_path):
    directory=tmp_path/'runs'/'old';directory.mkdir(parents=True)
    (directory/'app_job.json').write_text(json.dumps(dict(id='old',status='running',progress=35,model_dir='runs/old/bbox_1/model_1')))
    store=JobStore(tmp_path)
    assert store.jobs['old']['status']=='interrupted'
    assert store.model_dir(store.jobs['old'])==tmp_path/'runs'/'old'/'bbox_1'/'model_1'
    with pytest.raises(ValueError):
        store.model_dir(dict(id='bad',model_dir='../escape'))


def test_authenticated_api(tmp_path):
    store=JobStore(tmp_path)
    server=make_server(store);threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url+'/api/jobs')
        assert exc.value.code==403
        request=urllib.request.Request(url+'/api/jobs',headers={'X-Session-Token':store.token})
        assert json.load(urllib.request.urlopen(request))==[]
        request=urllib.request.Request(url+'/api/jobs',data=b'[]',headers={'X-Session-Token':store.token})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code==400
        request=urllib.request.Request(url+'/api/jobs',data=b'{}',headers={'X-Session-Token':store.token,'Origin':'https://other.example'})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code==403
    finally:
        server.shutdown();server.server_close()


def test_retry_reuses_only_finished_downloads_after_relocation(tmp_path):
    store=JobStore(tmp_path)
    bbox=[118.77,32.02,118.78,32.03]
    store.jobs['failed']=dict(id='failed',bbox=bbox,status='failed',phase='street')
    directory=store.runs/'failed'/'bbox_1'/'download'
    directory.mkdir(parents=True)
    for filename in ['elevation.tif','building.geoparquet','street.json']:
        (directory/filename).write_bytes(b'completed or partially written data')
    sources=store.download_sources(bbox)
    assert set(sources)=={'elevation','building'}
    assert all(Path(path).is_relative_to(tmp_path) for path in sources.values())
    assert store.download_sources([118.77,32.02,118.79,32.03])=={}
    (directory/'street.json').write_text(json.dumps(dict(directed=True, multigraph=True,
                                                        graph={}, nodes=[], links=[])))
    store.jobs['failed']['phase']='river'
    assert 'street' in store.download_sources(bbox)
