"""Isolated regression checks for rapid controls and cancellation races."""
from types import SimpleNamespace
import json
import errno
import importlib.util
import os
from pathlib import Path
import threading
import time

import pytest

import app
from execution_control import WorkerControl
from app import JobStore
from test_pause_jobs import ack, cooperative_worker, pending_job, wait_for


def test_rapid_pause_resume_cycles_keep_process_and_latest_intent(tmp_path, monkeypatch):
    cooperative_worker(tmp_path, monkeypatch)
    store = JobStore(tmp_path / 'data')
    job_id = store.create(dict(name='快速交错', bbox=[118.77, 32.02, 118.78, 32.03]))['id']
    try:
        job = store.jobs[job_id]
        wait_for(lambda: job.get('progress', 0) >= 2)
        proc = store.processes[job_id]
        for _ in range(3):
            for _ in range(6):
                store.pause(job_id)
                store.resume(job_id)
            store.pause(job_id)
            wait_for(lambda: job['status'] == 'paused')
            revision = job['_control_revision']
            assert job['_last_control_ack'] == revision
            command = json.loads((store.job_directory(job_id) / 'task-control.json').read_text())
            assert command == {'action': 'pause', 'revision': revision}
            before = job['progress']
            time.sleep(.3)
            assert job['status'] == 'paused' and job['progress'] == before
            assert store.processes[job_id] is proc and proc.poll() is None
            store.resume(job_id)
            wait_for(lambda: job['status'] == 'running' and job['progress'] > before)
        assert job['paused_seconds'] >= .9
        store.cancel(job_id)
        store.threads[job_id].join(timeout=10)
        assert job['status'] == 'cancelled'
        assert proc.poll() is not None
    finally:
        store.close()


@pytest.mark.parametrize('state', ['pausing', 'paused', 'resuming'])
def test_interrupted_pause_elapsed_is_stable_across_restarts(tmp_path, monkeypatch, state):
    clock = [1000.0]
    monkeypatch.setattr(app.time, 'time', lambda: clock[0])
    store = JobStore(tmp_path)
    job = pending_job(store)
    job.update(status=state, started=900, paused_since=990, paused_seconds=20)
    store.save(job)
    before = store.public(job)['elapsed']
    clock[0] += 3600
    restarted = JobStore(tmp_path)
    restored = restarted.jobs[job['id']]
    assert restored['status'] == 'interrupted'
    assert restarted.public(restored)['elapsed'] == before == 70
    clock[0] += 3600
    reopened = JobStore(tmp_path)
    assert reopened.public(reopened.jobs[job['id']])['elapsed'] == 70
    assert not reopened.cache_busy()


def test_latest_progress_message_survives_pending_control_cycles(tmp_path):
    store = JobStore(tmp_path)
    job = pending_job(store)
    store.pause(job['id'])
    store.resume(job['id'])
    store.pause(job['id'])
    store.consume(job, dict(job_id=job['id'], type='progress', percent=42,
                            phase='river', message='已完成道路，正在获取河流'))
    ack(store, job, 'running', 2, paused_seconds=0)
    assert job['status'] == 'pausing'
    ack(store, job, 'paused', 3, paused_at=time.time(), paused_seconds=0)
    store.resume(job['id'])
    ack(store, job, 'running', 4, paused_seconds=2)
    assert job['status'] == 'running'
    assert job['stage'] == '已完成道路，正在获取河流'
    assert job['progress'] == 42 and job['phase'] == 'river'


@pytest.mark.parametrize('ack_state,previous,expected', [
    ('paused', 'pausing', 'paused'),
    ('running', 'resuming', 'running'),
])
def test_failed_cancel_preserves_control_ack_received_during_termination(
        tmp_path, monkeypatch, ack_state, previous, expected):
    """A denied termination must not strand an already acknowledged control."""
    store = JobStore(tmp_path)
    job = pending_job(store)
    store.pause(job['id'])
    if previous == 'resuming':
        ack(store, job, 'paused', 1, paused_at=time.time()-2, paused_seconds=0)
        store.resume(job['id'])
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_terminate_job = store._terminate_job

    def denied_termination(proc):
        entered.set()
        assert release.wait(timeout=5)
        raise PermissionError('模拟进程终止被拒绝')

    def observed_terminate(*args):
        try:
            original_terminate_job(*args)
        finally:
            finished.set()

    monkeypatch.setattr(app, 'terminate_tree', denied_termination)
    monkeypatch.setattr(store, '_terminate_job', observed_terminate)
    store.processes[job['id']] = SimpleNamespace(poll=lambda: None)
    try:
        assert store.cancel(job['id'])['status'] == 'cancelling'
        assert entered.wait(timeout=5)
        ack(store, job, ack_state, job['_control_revision'],
            paused_at=time.time(), paused_seconds=2)
        # Incoming acknowledgements must not override cancellation in flight.
        assert job['status'] == 'cancelling'
        release.set()
        assert finished.wait(timeout=5)
        assert job['status'] == expected
        assert job['_last_control_ack'] == job['_control_revision']
    finally:
        release.set()
        finished.wait(timeout=5)
        store.processes.clear()


def test_control_read_recovers_from_short_windows_sharing_failure(tmp_path, monkeypatch):
    path = tmp_path / 'task-control.json'
    path.write_text('{"action":"resume","revision":2}', encoding='utf-8')
    original = Path.read_text
    attempts, waits, events = [], [], []
    def temporarily_locked(target, *args, **kwargs):
        attempts.append(target)
        if len(attempts) <= 2:
            raise PermissionError('temporary sharing lock')
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', temporarily_locked)
    WorkerControl(path, events.append, sleep=waits.append).checkpoint()
    assert len(attempts) == 3 and waits == [.02, .04]
    assert len(events) == 1 and events[0]['state'] == 'running'
    assert events[0]['revision'] == 2


@pytest.mark.parametrize('failure,retries', [(PermissionError('locked'), 5),
                                          (OSError(errno.EIO, 'storage error'), 0)])
def test_control_read_retries_are_bounded_and_do_not_mask_storage_errors(tmp_path, monkeypatch, failure, retries):
    waits, events = [], []
    def failed(*args, **kwargs):
        raise failure
    monkeypatch.setattr(Path, 'read_text', failed)
    with pytest.raises(RuntimeError, match='无法读取暂停/恢复命令'):
        WorkerControl(tmp_path / 'control', events.append, sleep=waits.append).checkpoint()
    assert len(waits) == retries and sum(waits) <= .63
    assert events == []


@pytest.mark.skipif(os.name != 'nt', reason='Requires native Windows sharing locks')
def test_control_read_recovers_after_real_exclusive_file_handle_is_released(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'packaging/validate_state_lock.py'
    spec = importlib.util.spec_from_file_location('native_pause_lock_qa', script)
    qa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qa)
    path = tmp_path / 'task-control.json'
    path.write_text('{"action":"resume","revision":2}', encoding='utf-8')
    held = qa.NoDeleteShare(path)
    held.close()
    held.handle = held.kernel.CreateFileW(str(path.resolve()), 0x80000000, 0, None, 3, 0x80, None)
    assert held.handle != qa.wintypes.HANDLE(-1).value
    events = []
    try:
        with pytest.raises(PermissionError):
            path.read_text()
        timer = threading.Timer(.08, held.close)
        timer.start()
        WorkerControl(path, events.append).checkpoint()
        timer.join(timeout=2)
        assert not timer.is_alive()
        assert len(events) == 1 and events[0]['revision'] == 2
    finally:
        held.close()
