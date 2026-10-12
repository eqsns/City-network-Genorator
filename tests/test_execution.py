import json
import sys
import threading
import time
from pathlib import Path
import pytest
import app
from app import JobStore, ACTIVE


def await_end(store,job_id):
    deadline=time.monotonic()+15
    while store.jobs[job_id]['status'] in ACTIVE and time.monotonic()<deadline:
        time.sleep(.05)
    # Terminal state can be visible in memory while its snapshot is flushing.
    # Wait for the worker supervisor's finally block before checking cleanup.
    store.threads[job_id].join(timeout=max(0, deadline-time.monotonic()))
    assert not store.threads[job_id].is_alive()
    return store.jobs[job_id]


def fake_worker(tmp_path,monkeypatch,body):
    script=tmp_path/'fake_worker.py'
    script.write_text('import sys,json,time\nfrom pathlib import Path\nconfig,events,job,title=sys.argv[1:]\n'+body,encoding='utf-8')
    monkeypatch.setattr(app,'worker_command',lambda *args:[sys.executable,str(script),*map(str,args)])


def test_failed_worker_never_completes(tmp_path,monkeypatch):
    fake_worker(tmp_path,monkeypatch,"Path(events).write_text(json.dumps(dict(job_id=job,type='progress',percent=100,phase='write',message='writing'))+'\\n')\nsys.exit(3)\n")
    store=JobStore(tmp_path/'data')
    job=store.create(dict(name='失败测试',bbox=[118.77,32.02,118.78,32.03]))
    result=await_end(store,job['id'])
    assert result['status']=='failed'
    assert result['progress']==99
    assert store.file(result,'log').exists()


def test_cancel_and_single_running_task(tmp_path,monkeypatch):
    fake_worker(tmp_path,monkeypatch,'time.sleep(60)\n')
    store=JobStore(tmp_path/'data')
    job=store.create(dict(name='取消测试',bbox=[118.77,32.02,118.78,32.03]))
    with pytest.raises(RuntimeError):
        store.create(dict(name='重复测试',bbox=[118.77,32.02,118.78,32.03]))
    store.cancel(job['id'])
    result=await_end(store,job['id'])
    assert result['status']=='cancelled'
    assert result['progress']<100
    assert not store.processes


def test_zero_exit_without_artifacts_is_failure(tmp_path,monkeypatch):
    fake_worker(tmp_path,monkeypatch,"Path(events).write_text(json.dumps(dict(job_id=job,type='result',stats={'nodes':5}))+'\\n')\n")
    store=JobStore(tmp_path/'data')
    job=store.create(dict(name='缺少成果',bbox=[118.77,32.02,118.78,32.03]))
    assert await_end(store,job['id'])['status']=='failed'


def test_cancel_returns_without_waiting_and_kills_stalled_worker(tmp_path,monkeypatch):
    fake_worker(tmp_path,monkeypatch,'time.sleep(90)\n')
    original_terminate = app.terminate_tree
    entered, release = threading.Event(), threading.Event()

    def delayed_terminate(proc):
        entered.set()
        release.wait(timeout=5)
        original_terminate(proc)

    monkeypatch.setattr(app, 'terminate_tree', delayed_terminate)
    store = JobStore(tmp_path/'data')
    job = store.create(dict(name='网络阻塞时取消',bbox=[118.77,32.02,118.78,32.03]))
    job_id = job['id']
    try:
        deadline = time.monotonic() + 10
        while job_id not in store.processes and time.monotonic() < deadline:
            time.sleep(.02)
        proc = store.processes[job_id]
        assert proc.poll() is None
        started = time.monotonic()
        result = store.cancel(job_id)
        elapsed = time.monotonic() - started
        assert entered.wait(timeout=2)
        assert elapsed < 1, 'cancel must return while process termination is still blocked'
        assert result['status'] == 'cancelling'
        assert proc.poll() is None
        # A late worker event must never turn a cancelled job into a completed one.
        store.consume(store.jobs[job_id], dict(job_id=job_id,type='progress',percent=100,
                                              phase='write',message='late progress'))
        assert store.jobs[job_id]['progress'] < 100
    finally:
        release.set()
        store.close()
    assert proc.poll() is not None
    assert store.jobs[job_id]['status'] == 'cancelled'
    assert store.jobs[job_id]['progress'] < 100
    assert job_id not in store.processes
    assert store.file(store.jobs[job_id], 'log').is_file()
    assert JobStore(store.data).jobs[job_id]['status'] == 'cancelled'
