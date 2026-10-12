"""Persistence faults must not turn a healthy calculation into a failed job.

All files and subprocesses belong to pytest's temporary directory.  The worker
below only emits fixture events and tiny output files; it performs no download.
"""
import errno
import json
import os
import sys
import time
from pathlib import Path

import pytest

import app
from app import JobStore
import persistence


def windows_lock(code=5):
    error = PermissionError(errno.EACCES, 'test destination temporarily locked')
    error.winerror = code
    return error


@pytest.mark.parametrize('code', [5, 32, 33])
def test_atomic_json_retries_windows_locks_without_damaging_old_file(tmp_path, monkeypatch, code):
    target = tmp_path / '中文 空格' / 'app_job.json'
    target.parent.mkdir()
    old = {'status': 'running', 'progress': 25}
    new = {'status': 'complete', 'progress': 100, 'name': '测试成果'}
    target.write_text(json.dumps(old), encoding='utf-8')
    replace = persistence.os.replace
    calls, waits = [], []

    def temporarily_locked(source, destination):
        source, destination = Path(source), Path(destination)
        assert destination == target
        assert source.parent == target.parent
        assert json.loads(target.read_text(encoding='utf-8')) == old
        assert json.loads(source.read_text(encoding='utf-8')) == new
        calls.append(source)
        if len(calls) < 3:
            raise windows_lock(code)
        return replace(source, destination)

    monkeypatch.setattr(persistence.os, 'replace', temporarily_locked)
    monkeypatch.setattr(persistence.time, 'sleep', waits.append)
    persistence.atomic_write_json(target, new)
    assert len(calls) == 3
    assert len(waits) == 2 and all(0 < delay <= 1 for delay in waits)
    assert json.loads(target.read_text(encoding='utf-8')) == new
    assert list(target.parent.iterdir()) == [target]


def test_persistent_windows_lock_is_bounded_and_cleans_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / 'app_job.json'
    target.write_text('{"status":"running"}', encoding='utf-8')
    before = target.read_bytes()
    calls, waits = [], []

    def locked(source, destination):
        calls.append(Path(source))
        raise windows_lock()

    monkeypatch.setattr(persistence.os, 'replace', locked)
    monkeypatch.setattr(persistence.time, 'sleep', waits.append)
    with pytest.raises(PermissionError):
        persistence.atomic_write_json(target, {'status': 'complete'})
    assert 1 < len(calls) <= 12
    assert 0 < sum(waits) <= 5, 'state saving must not block cancellation indefinitely'
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_nontransient_disk_error_is_not_retried_or_hidden(tmp_path, monkeypatch):
    target = tmp_path / 'app_job.json'
    target.write_text('{"progress":25}', encoding='utf-8')
    before = target.read_bytes()
    calls, waits = [], []

    def disk_full(source, destination):
        calls.append(source)
        raise OSError(errno.ENOSPC, 'test disk full')

    monkeypatch.setattr(persistence.os, 'replace', disk_full)
    monkeypatch.setattr(persistence.time, 'sleep', waits.append)
    with pytest.raises(OSError) as caught:
        persistence.atomic_write_json(target, {'progress': 35})
    assert caught.value.errno == errno.ENOSPC
    assert len(calls) == 1 and not waits
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_unserializable_data_leaves_previous_json_and_no_temporary_file(tmp_path):
    target = tmp_path / 'app_job.json'
    target.write_text('{"status":"running"}', encoding='utf-8')
    before = target.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        persistence.atomic_write_json(target, {'not_json': object()})
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def running_job(store):
    job = dict(id='fixture', name='临时测试', status='running', progress=5,
               stage='准备配置', created_at='2026-10-08', started=time.time(),
               bbox=[118.77, 32.02, 118.78, 32.03], stats={},
               model_dir='runs/fixture/bbox_1/model_1')
    store.jobs[job['id']] = job
    store.save(job)
    return job


def test_failed_progress_save_keeps_live_state_and_later_event_repairs_disk(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = running_job(store)
    save = store.save
    failures = [True]

    def once_locked(value):
        if failures:
            failures.pop()
            raise windows_lock()
        return save(value)

    monkeypatch.setattr(store, 'save', once_locked)
    store.consume(job, dict(job_id='fixture', type='progress', percent=25,
                            phase='street', message='正在下载道路'))
    assert job['status'] == 'running' and job['progress'] == 25
    assert job.get('persistence_warning')
    metadata = store.job_directory(job['id']) / 'app_job.json'
    assert json.loads(metadata.read_text(encoding='utf-8'))['progress'] == 5
    store.consume(job, dict(job_id='fixture', type='progress', percent=35,
                            phase='river', message='正在下载河流'))
    assert not job.get('persistence_warning')
    saved = json.loads(metadata.read_text(encoding='utf-8'))
    assert saved['progress'] == 35 and saved['status'] == 'running'
    assert not saved.get('persistence_warning')


def test_failed_initial_save_does_not_register_or_launch_a_job(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    launched = []
    monkeypatch.setattr(app.threading.Thread, 'start', lambda self: launched.append(self))

    def cannot_save(job):
        raise OSError(errno.ENOSPC, 'test disk full')

    monkeypatch.setattr(store, 'save', cannot_save)
    with pytest.raises(OSError):
        store.create(dict(name='不能保存', bbox=[118.77, 32.02, 118.78, 32.03]))
    assert not launched
    assert not store.jobs and not store.processes and not store.threads


def test_failed_rename_reports_error_and_retains_original_name(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = running_job(store)
    original = job['name']

    def cannot_save(value):
        raise windows_lock()

    monkeypatch.setattr(store, 'save', cannot_save)
    with pytest.raises(OSError):
        store.rename(job['id'], {'name': '未保存的新名称'})
    assert job['name'] == original
    metadata = store.job_directory(job['id']) / 'app_job.json'
    assert json.loads(metadata.read_text(encoding='utf-8'))['name'] == original


def install_fixture_worker(tmp_path, monkeypatch):
    script = tmp_path / 'fixture_worker.py'
    script.write_text('''import json, sys, time
from pathlib import Path
config, events, job, title = sys.argv[1:]
with Path(events).open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(dict(job_id=job, type='progress', percent=25,
                                phase='street', message='fixture download')) + '\\n')
    stream.flush()
    time.sleep(.5)
    model = Path(config).parent / 'bbox_1' / 'model_1'
    model.mkdir(parents=True)
    for name in ('model_1.inp', 'network.png', 'network.geojson', 'SWMM-model.zip'):
        (model / name).write_bytes(b'fixture output')
    stream.write(json.dumps(dict(job_id=job, type='result', stats={'nodes': 3})) + '\\n')
    stream.flush()
''', encoding='utf-8')
    monkeypatch.setattr(app, 'worker_command',
                        lambda *args: [sys.executable, str(script), *map(str, args)])


def wait_worker(store, job_id):
    thread = store.threads[job_id]
    thread.join(timeout=10)
    assert not thread.is_alive(), 'fixture worker did not stop within the test deadline'
    return store.jobs[job_id]


def test_progress_persistence_failure_does_not_terminate_healthy_worker(tmp_path, monkeypatch):
    install_fixture_worker(tmp_path, monkeypatch)
    store = JobStore(tmp_path / 'data')
    save = store.save
    failed = []
    terminated = []
    terminate = app.terminate_tree

    def progress_once_locked(job):
        if job.get('phase') == 'street' and not failed:
            failed.append(True)
            raise windows_lock()
        return save(job)

    def record_termination(proc):
        terminated.append(proc)
        return terminate(proc)

    monkeypatch.setattr(store, 'save', progress_once_locked)
    monkeypatch.setattr(app, 'terminate_tree', record_termination)
    created = store.create(dict(name='进度保存故障', bbox=[118.77, 32.02, 118.78, 32.03]))
    result = wait_worker(store, created['id'])
    assert failed and not terminated
    assert result['status'] == 'complete' and result['progress'] == 100
    assert not result.get('persistence_warning')
    assert store.file(result, 'inp').is_file()
    assert JobStore(store.data).jobs[result['id']]['status'] == 'complete'


def test_terminal_persistence_failure_is_visible_and_can_be_flushed(tmp_path, monkeypatch):
    install_fixture_worker(tmp_path, monkeypatch)
    store = JobStore(tmp_path / 'data')
    save = store.save
    failures = []

    def terminal_locked(job):
        if job['status'] == 'complete':
            failures.append(True)
            raise windows_lock()
        return save(job)

    monkeypatch.setattr(store, 'save', terminal_locked)
    created = store.create(dict(name='完成状态保存故障', bbox=[118.77, 32.02, 118.78, 32.03]))
    result = wait_worker(store, created['id'])
    assert failures
    assert result['status'] == 'complete' and result['progress'] == 100
    assert store.public(result).get('persistence_warning')
    assert store.file(result, 'inp').is_file()
    monkeypatch.setattr(store, 'save', save)
    store.flush_pending(force=True)
    assert not result.get('persistence_warning')
    assert JobStore(store.data).jobs[result['id']]['status'] == 'complete'


def test_terminal_recovery_snapshot_restores_real_success_after_restart(tmp_path, monkeypatch):
    install_fixture_worker(tmp_path, monkeypatch)
    store = JobStore(tmp_path / 'data')
    save = store.save

    def terminal_locked(job):
        if job['status'] == 'complete':
            raise windows_lock()
        return save(job)

    monkeypatch.setattr(store, 'save', terminal_locked)
    created = store.create(dict(name='备份恢复完成任务', bbox=[118.77, 32.02, 118.78, 32.03]))
    result = wait_worker(store, created['id'])
    folder = store.job_directory(result['id'])
    assert result['status'] == 'complete' and result.get('persistence_warning')
    assert json.loads((folder / 'app_job.json').read_text(encoding='utf-8'))['status'] == 'running'
    recovery = folder / 'app_job.recovery.json'
    assert json.loads(recovery.read_text(encoding='utf-8'))['status'] == 'complete'
    # A new app instance has no knowledge of the prior in-memory pending write.
    restarted = JobStore(store.data)
    restored = restarted.jobs[result['id']]
    assert restored['status'] == 'complete' and restored['progress'] == 100
    assert restored['stats'] == {'nodes': 3}
    assert not restored.get('persistence_warning')
    assert json.loads((folder / 'app_job.json').read_text(encoding='utf-8'))['status'] == 'complete'
    assert not recovery.exists()


def test_restart_can_recover_when_main_snapshot_is_unreadable(tmp_path):
    folder = tmp_path / 'runs' / 'recovered'
    folder.mkdir(parents=True)
    (folder / 'app_job.json').write_text('{"status":', encoding='utf-8')
    recovered = dict(id='recovered', name='完整的恢复记录', status='failed', progress=25,
                     stage='计算失败', error='真实计算错误', created_at='2026-10-08',
                     bbox=[118.77, 32.02, 118.78, 32.03], stats={})
    (folder / 'app_job.recovery.json').write_text(json.dumps(recovered), encoding='utf-8')
    store = JobStore(tmp_path)
    assert store.jobs['recovered']['status'] == 'failed'
    assert store.jobs['recovered']['error'] == '真实计算错误'
    assert json.loads((folder / 'app_job.json').read_text(encoding='utf-8'))['status'] == 'failed'


def test_cancel_still_terminates_when_cancelling_state_cannot_be_saved(tmp_path, monkeypatch):
    install_fixture_worker(tmp_path, monkeypatch)
    store = JobStore(tmp_path / 'data')
    save = store.save
    failures = []

    def cancelling_locked(job):
        if job['status'] == 'cancelling':
            failures.append(True)
            raise windows_lock()
        return save(job)

    monkeypatch.setattr(store, 'save', cancelling_locked)
    created = store.create(dict(name='保存受阻仍可取消', bbox=[118.77, 32.02, 118.78, 32.03]))
    job_id = created['id']
    deadline = time.monotonic() + 5
    while job_id not in store.processes and time.monotonic() < deadline:
        time.sleep(.01)
    proc = store.processes[job_id]
    try:
        result = store.cancel(job_id)
        assert failures
        assert result['status'] == 'cancelling'
        result = wait_worker(store, job_id)
        assert result['status'] == 'cancelled' and result['progress'] < 100
        assert proc.poll() is not None
        assert not result.get('persistence_warning')
        assert JobStore(store.data).jobs[job_id]['status'] == 'cancelled'
    finally:
        app.terminate_tree(proc)


@pytest.mark.parametrize('newer_snapshot', ['app_job.json', 'app_job.recovery.json'])
def test_restart_uses_revision_when_snapshot_timestamps_are_identical(tmp_path, newer_snapshot):
    folder = tmp_path / 'runs' / 'revision'
    folder.mkdir(parents=True)
    running = dict(id='revision', name='版本恢复', status='running', progress=25,
                   stage='正在下载', created_at='2026-10-08', stats={}, _state_revision=2)
    complete = dict(running, status='complete', progress=100, stage='生成完成',
                    stats={'nodes': 3}, _state_revision=3)
    fixed_timestamp = 1_790_000_000_000_000_000
    for name in ('app_job.json', 'app_job.recovery.json'):
        path = folder / name
        value = complete if name == newer_snapshot else running
        path.write_text(json.dumps(value), encoding='utf-8')
        os.utime(path, ns=(fixed_timestamp, fixed_timestamp))
    assert (folder / 'app_job.json').stat().st_mtime_ns == (folder / 'app_job.recovery.json').stat().st_mtime_ns
    restarted = JobStore(tmp_path)
    job = restarted.jobs['revision']
    assert job['status'] == 'complete' and job['progress'] == 100
    assert job['stats'] == {'nodes': 3}
    assert '_state_revision' not in restarted.public(job)
    canonical = json.loads((folder / 'app_job.json').read_text(encoding='utf-8'))
    assert canonical['status'] == 'complete'
    assert canonical['_state_revision'] >= 3


def test_old_recovery_file_left_by_unlink_error_cannot_override_success(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = running_job(store)
    folder = store.job_directory(job['id'])
    recovery = folder / 'app_job.recovery.json'
    # This is the last durable snapshot before the successful completion save.
    job['_state_revision'] = 2
    recovery.write_text(json.dumps(job), encoding='utf-8')
    unlink = Path.unlink
    blocked = []

    def cannot_clean_recovery(path, *args, **kwargs):
        if path == recovery:
            blocked.append(path)
            raise windows_lock()
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', cannot_clean_recovery)
    store.update(job, status='complete', progress=100, stats={'nodes': 3})
    assert blocked and recovery.is_file()
    assert not job.get('persistence_warning')
    canonical = folder / 'app_job.json'
    timestamp = canonical.stat().st_mtime_ns
    # Coarse filesystem times or a clock adjustment must not resurrect running.
    os.utime(recovery, ns=(timestamp, timestamp))
    restarted = JobStore(tmp_path)
    assert restarted.jobs[job['id']]['status'] == 'complete'
    assert restarted.jobs[job['id']]['progress'] == 100


def test_both_snapshots_full_keeps_real_success_warns_and_flushes_later(tmp_path, monkeypatch):
    install_fixture_worker(tmp_path, monkeypatch)
    store = JobStore(tmp_path / 'data')
    writer = app.atomic_write_json
    failed_paths = []

    def no_space_at_completion(path, data):
        if data.get('status') == 'complete':
            failed_paths.append(Path(path).name)
            raise OSError(errno.ENOSPC, 'test disk full for both state snapshots')
        return writer(path, data)

    monkeypatch.setattr(app, 'atomic_write_json', no_space_at_completion)
    created = store.create(dict(name='两份状态均保存失败', bbox=[118.77, 32.02, 118.78, 32.03]))
    job = wait_worker(store, created['id'])
    assert {'app_job.json', 'app_job.recovery.json'} <= set(failed_paths)
    assert job['status'] == 'complete' and job['progress'] == 100
    assert store.public(job).get('persistence_warning')
    assert job['id'] in store.pending_saves
    assert store.file(job, 'inp').is_file()
    assert job['id'] not in store.processes
    monkeypatch.setattr(app, 'atomic_write_json', writer)
    store.flush_pending(force=True)
    assert job['id'] not in store.pending_saves
    assert not job.get('persistence_warning')
    assert JobStore(store.data).jobs[job['id']]['status'] == 'complete'


def test_events_file_creation_failure_marks_failed_without_starting_worker(tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'data')
    touch = Path.touch
    launches = []

    def cannot_create_events(path, *args, **kwargs):
        if path.name == 'events.jsonl':
            raise windows_lock()
        return touch(path, *args, **kwargs)

    def unexpected_worker(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError('worker must not start without an event stream')

    monkeypatch.setattr(Path, 'touch', cannot_create_events)
    monkeypatch.setattr(app.subprocess, 'Popen', unexpected_worker)
    created = store.create(dict(name='事件文件无法创建', bbox=[118.77, 32.02, 118.78, 32.03]))
    job = wait_worker(store, created['id'])
    assert not launches
    assert job['status'] == 'failed' and job['progress'] < 100
    assert 'locked' in job['error']
    assert not store.processes
    assert JobStore(store.data).jobs[job['id']]['status'] == 'failed'
