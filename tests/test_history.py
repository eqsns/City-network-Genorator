"""History mutations operate on real temporary output trees, never user data."""
import json
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from app import JobStore, make_server


def finished_job(store, job_id='finished', name='原名称'):
    job = dict(id=job_id, name=name, status='complete', progress=100,
               stage='生成完成', created_at='2026-10-05', started=1, ended=2,
               bbox=[118.77, 32.02, 118.78, 32.03], stats={'nodes': 2},
               model_dir=f'runs/{job_id}/bbox_1/model_1')
    store.jobs[job_id] = job
    store.save(job)
    model = store.model_dir(job)
    model.mkdir(parents=True)
    (model / 'network.geojson').write_text('{"type":"FeatureCollection","features":[]}')
    (model / 'model_1.inp').write_text('[TITLE]\nsynthetic model')
    (store.job_directory(job_id) / 'run.log').write_text('completed')
    return job


def test_rename_preserves_outputs_and_survives_restart(tmp_path):
    store = JobStore(tmp_path)
    job = finished_job(store)
    before = store.file(job, 'inp').read_bytes()
    renamed = store.rename(job['id'], {'name': '  城西 新项目  '})
    assert renamed['name'] == '城西 新项目'
    assert renamed['id'] == job['id']
    assert renamed['progress'] == 100
    assert store.file(job, 'inp').read_bytes() == before
    restarted = JobStore(tmp_path)
    assert restarted.jobs[job['id']]['name'] == '城西 新项目'
    assert restarted.file(restarted.jobs[job['id']], 'inp').read_bytes() == before


@pytest.mark.parametrize('payload', [None, [], {}, {'name': None}, {'name': 4},
                                    {'name': ''}, {'name': '  '}, {'name': '字' * 51},
                                    {'name': '坏\n名称'}, {'name': '坏\x00名称'}])
def test_invalid_rename_leaves_original_intact(tmp_path, payload):
    store = JobStore(tmp_path)
    job = finished_job(store)
    metadata = store.job_directory(job['id']) / 'app_job.json'
    before = metadata.read_bytes()
    with pytest.raises(ValueError):
        store.rename(job['id'], payload)
    assert job['name'] == '原名称'
    assert metadata.read_bytes() == before


def test_delete_removes_outputs_and_stays_deleted_after_restart(tmp_path):
    store = JobStore(tmp_path)
    deleted = finished_job(store)
    kept = finished_job(store, 'kept', '保留项目')
    target = store.job_directory(deleted['id'])
    assert store.delete(deleted['id']) == {'deleted': deleted['id']}
    assert not target.exists()
    assert deleted['id'] not in store.jobs
    restarted = JobStore(tmp_path)
    assert set(restarted.jobs) == {'kept'}
    assert restarted.file(kept, 'inp').is_file()


def test_sample_rename_and_delete_persist(tmp_path):
    sample = tmp_path / 'sample'
    sample.mkdir()
    (sample / 'network.geojson').write_text('{"type":"FeatureCollection","features":[]}')
    (sample / 'stats.json').write_text('{"nodes":2}')
    store = JobStore(tmp_path)
    assert store.jobs['sample']['name'] == '南京主城区核心范围'
    store.rename('sample', {'name': '我的南京成果'})
    restarted = JobStore(tmp_path)
    assert restarted.jobs['sample']['name'] == '我的南京成果'
    restarted.delete('sample')
    assert not sample.exists()
    assert 'sample' not in JobStore(tmp_path).jobs


@pytest.mark.parametrize('status', ['running', 'cancelling'])
def test_cannot_delete_active_job(tmp_path, status):
    store = JobStore(tmp_path)
    job = finished_job(store)
    store.update(job, status=status)
    with pytest.raises(RuntimeError, match='停止'):
        store.delete(job['id'])
    assert store.file(job, 'inp').is_file()
    assert job['id'] in store.jobs


def test_cannot_delete_downloads_reused_by_active_worker(tmp_path):
    store = JobStore(tmp_path)
    previous = finished_job(store)
    current = finished_job(store, 'current')
    store.update(current, status='running')
    source = store.job_directory(previous['id']) / 'bbox_1' / 'download' / 'street.json'
    source.parent.mkdir(parents=True)
    graph_json = json.dumps(dict(directed=True, multigraph=True, graph={}, nodes=[], links=[]))
    source.write_text(graph_json)
    config = store.job_directory(current['id']) / 'config.yml'
    config.write_text(yaml.safe_dump({'address_overrides': {'street': str(source)}}))
    with pytest.raises(RuntimeError, match='复用'):
        store.delete(previous['id'])
    assert source.read_text() == graph_json
    store.update(current, status='cancelled')
    store.delete(previous['id'])
    assert not source.exists()
    assert store.job_directory(current['id']).exists()


@pytest.fixture
def api(tmp_path):
    store = JobStore(tmp_path)
    finished_job(store)
    server = make_server(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('action', ['rename', 'delete', 'cancel'])
@pytest.mark.parametrize('bad_access', ['missing_token', 'wrong_origin'])
def test_history_mutations_require_session_and_local_origin(api, action, bad_access):
    store, base = api
    headers = {'X-Session-Token': store.token, 'Origin': base}
    if bad_access == 'missing_token':
        del headers['X-Session-Token']
    else:
        headers['Origin'] = 'https://unrelated.example'
    request = urllib.request.Request(base + '/api/jobs/finished/' + action,
                                     data=b'{"name":"changed"}', headers=headers)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 403
    assert store.jobs['finished']['name'] == '原名称'
    assert store.file(store.jobs['finished'], 'inp').is_file()


def test_authenticated_rename_and_delete_api(api):
    store, base = api
    headers = {'X-Session-Token': store.token, 'Origin': base}
    request = urllib.request.Request(base + '/api/jobs/finished/rename',
                                     data=json.dumps({'name': '新名称'}).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.load(response)['name'] == '新名称'
    request = urllib.request.Request(base + '/api/jobs/finished/delete', data=b'{}', headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.load(response) == {'deleted': 'finished'}
    request = urllib.request.Request(base + '/api/jobs/finished', headers=headers)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 404
