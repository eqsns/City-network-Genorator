"""Pause races, real worker continuity and safe cache mutations in temporary data."""
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import app
from app import JobStore, make_server
from test_history import finished_job


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.03)
    raise AssertionError('Timed out waiting for worker state')


def cooperative_worker(tmp_path, monkeypatch):
    script = tmp_path / 'cooperative.py'
    script.write_text('''import sys,json,time
from pathlib import Path
sys.path.insert(0, ROOT)
from execution_control import WorkerControl
config,events,job,title=sys.argv[1:]
def send(event):
    event['job_id']=job
    with Path(events).open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(event)+'\\n')
control=WorkerControl(Path(config).parent/'task-control.json',send)
for step in range(500):
    control.checkpoint()
    send(dict(type='progress',percent=min(95,step+1),phase='graph',message='step '+str(step)))
    (Path(config).parent/'ticks').write_text(str(step))
    time.sleep(.08)
'''.replace('ROOT', repr(str(Path(app.__file__).parent))), encoding='utf-8')
    monkeypatch.setattr(app, 'worker_command', lambda *args: [sys.executable, str(script), *map(str, args)])


def test_pause_keeps_same_worker_freezes_progress_and_allows_cancel(tmp_path, monkeypatch):
    cooperative_worker(tmp_path, monkeypatch)
    store = JobStore(tmp_path / 'data')
    job_id = store.create(dict(name='暂停测试', bbox=[118.77,32.02,118.78,32.03]))['id']
    try:
        job = store.jobs[job_id]
        wait_for(lambda: job.get('progress', 0) >= 2)
        proc = store.processes[job_id]
        assert store.pause(job_id)['status'] == 'pausing'
        wait_for(lambda: job['status'] == 'paused')
        tick = (store.job_directory(job_id) / 'ticks').read_text()
        progress = job['progress']
        elapsed = store.public(job)['elapsed']
        time.sleep(1.1)
        assert (store.job_directory(job_id) / 'ticks').read_text() == tick
        assert job['progress'] == progress
        assert store.public(job)['elapsed'] == elapsed
        assert proc.poll() is None
        with pytest.raises(RuntimeError):
            store.create(dict(name='不能并行', bbox=[118.77,32.02,118.78,32.03]))
        with pytest.raises(RuntimeError):
            store.clear_cache({'generation': True})
        with pytest.raises(RuntimeError):
            store.delete(job_id)
        assert store.resume(job_id)['status'] == 'resuming'
        wait_for(lambda: job['status'] == 'running' and job['progress'] > progress)
        assert store.processes[job_id] is proc
        assert job['paused_seconds'] >= 1
        store.pause(job_id)
        wait_for(lambda: job['status'] == 'paused')
        store.cancel(job_id)
        store.threads[job_id].join(timeout=10)
        assert not store.threads[job_id].is_alive()
        assert job['status'] == 'cancelled' and job['progress'] < 100
        assert proc.poll() is not None
    finally:
        store.close()


def pending_job(store):
    job = finished_job(store)
    job.update(status='running', progress=25, stage='正在下载', started=time.time()-10)
    job.pop('ended')
    return job


def ack(store, job, state, revision, **fields):
    store.consume(job, dict(job_id=job['id'], type='control', state=state, revision=revision, **fields))


def test_newer_control_intent_wins_and_stale_ack_does_not_double_count_pause(tmp_path):
    store = JobStore(tmp_path)
    job = pending_job(store)
    store.pause(job['id'])
    ack(store, job, 'paused', 1, paused_at=time.time()-3, paused_seconds=0)
    store.resume(job['id'])
    store.pause(job['id'])
    ack(store, job, 'running', 2, paused_seconds=3)
    assert job['status'] == 'pausing' and 'paused_since' not in job
    ack(store, job, 'paused', 3, paused_at=time.time(), paused_seconds=3)
    assert job['status'] == 'paused' and job['paused_seconds'] == 3
    ack(store, job, 'running', 2, paused_seconds=3)
    assert job['status'] == 'paused' and 'paused_since' in job
    store.resume(job['id'])
    ack(store, job, 'running', 4, paused_seconds=4)
    assert job['status'] == 'running' and 'paused_since' not in job


def test_pause_command_write_error_leaves_worker_state_unchanged(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = pending_job(store)
    before = job.copy()
    monkeypatch.setattr(app, 'atomic_write_json', lambda *a: (_ for _ in ()).throw(PermissionError('locked')))
    with pytest.raises(PermissionError):
        store.pause(job['id'])
    assert job == before


@pytest.mark.parametrize('status', ['pausing', 'paused', 'resuming'])
def test_restart_marks_paused_process_as_interrupted(tmp_path, status):
    store = JobStore(tmp_path)
    job = pending_job(store)
    job['status'] = status
    store.save(job)
    assert JobStore(tmp_path).jobs[job['id']]['status'] == 'interrupted'


def test_delete_keep_cache_preserves_complete_inputs_but_not_partial(tmp_path):
    store = JobStore(tmp_path)
    job = finished_job(store)
    job.update(status='failed', phase='street')
    download = store.job_directory(job['id'])/'bbox_1/download'
    download.mkdir()
    for filename in app.DOWNLOADS.values():
        (download / filename).write_bytes(b'input')
    store.delete(job['id'], clear_cache=False)
    cached = list((tmp_path/'cache').glob('*'))[0]
    assert {p.name for p in cached.iterdir()} == {'elevation.tif','building.geoparquet'}
    assert not store.jobs


def test_delete_with_cache_clears_shared_downloads_but_keeps_other_models(tmp_path):
    store = JobStore(tmp_path)
    first, second = finished_job(store), finished_job(store, 'second')
    for job in (first, second):
        directory = store.job_directory(job['id'])/'bbox_1/download'
        directory.mkdir()
        (directory/'street.json').write_bytes(b'input')
    cache = tmp_path/'cache'
    cache.mkdir(); (cache/'osm.json').write_bytes(b'osm')
    store.delete(first['id'], clear_cache=True)
    assert not cache.exists()
    assert not (store.job_directory(second['id'])/'bbox_1/download').exists()
    assert store.file(second, 'inp').is_file()
    assert JobStore(tmp_path).jobs[second['id']]['status'] == 'complete'


def test_pause_and_cache_api_authorization_and_mutations(tmp_path):
    store = JobStore(tmp_path)
    job = pending_job(store)
    server = make_server(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, payload=None, token=True, origin=None):
        headers = {'Origin': origin or base}
        if token: headers['X-Session-Token'] = store.token
        data = None if payload is None else json.dumps(payload).encode()
        return urllib.request.urlopen(urllib.request.Request(base+path, data=data, headers=headers), timeout=5)
    try:
        for path, payload in [('/api/cache', None), ('/api/cache/clear', {'generation':True}),
                              ('/api/jobs/finished/pause', {}), ('/api/jobs/finished/resume', {})]:
            with pytest.raises(urllib.error.HTTPError) as exc: request(path, payload, token=False)
            assert exc.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            request('/api/cache/clear', {'generation':True}, origin='https://elsewhere.test')
        assert exc.value.code == 403
        with request('/api/jobs/finished/pause', {}) as response:
            assert json.load(response)['status'] == 'pausing'
        with request('/api/jobs/finished/resume', {}) as response:
            assert json.load(response)['status'] == 'resuming'
        with request('/api/cache') as response: assert json.load(response)['busy']
        with pytest.raises(urllib.error.HTTPError) as exc: request('/api/cache/clear', {'generation':True})
        assert exc.value.code == 409
        job['status'] = 'complete'
        cache = tmp_path/'cache'; cache.mkdir(); (cache/'old.json').write_bytes(b'123')
        with request('/api/cache/clear', {'generation':True}) as response:
            assert json.load(response)['cleared_bytes'] == 3
        assert store.file(job, 'inp').is_file()
        with pytest.raises(urllib.error.HTTPError) as exc: request('/api/cache/clear', {'generation':'true'})
        assert exc.value.code == 400
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
