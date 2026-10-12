"""Windows sharing-lock QA for state persistence, using a fixture worker.

No geography is downloaded and no hydraulic/model generation is performed.
All artifacts are isolated below .cache/状态写入验收/<unique run>.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture_worker(config, events, job_id, title):
    """A real subprocess that emits events and explicitly fake output files."""
    folder = Path(config).parent
    deadline = time.monotonic() + 20
    while not (folder / 'fixture-go').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('Fixture gate was not released')
        time.sleep(.02)
    (folder / 'fixture-started.json').write_text(
        json.dumps({'pid': os.getpid(), 'fixture_only': True}), encoding='utf-8')

    def send(event):
        with Path(events).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(event, job_id=job_id), ensure_ascii=False) + '\n')

    for percent in (25, 45, 90, 99):
        send(dict(type='progress', phase='verify', percent=percent,
                  message=f'文件锁验收 fixture {percent}%，不是管网生成'))
        time.sleep(.2)
    model = folder / 'bbox_1' / 'model_1'
    model.mkdir(parents=True)
    for filename in ('model_fixture.inp', 'network.png', 'network.geojson', 'SWMM-model.zip'):
        (model / filename).write_text('QA fixture only; not a usable model.', encoding='utf-8')
    send(dict(type='result', stats={'fixture_only': True, 'worker_pid': os.getpid()}))
    (folder / 'fixture-exited.json').write_text(
        json.dumps({'pid': os.getpid(), 'normal_exit': True}), encoding='utf-8')
    return 0


class NoDeleteShare:
    """Open a real Windows handle that permits reads/writes but denies replace."""
    def __init__(self, path):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateFileW(str(Path(path).resolve()), 0x80000000,
            0x1 | 0x2, None, 3, 0x80, None)
        if self.handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            if not self.kernel.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def validate():
    if os.name != 'nt':
        raise RuntimeError('This QA requires Windows file sharing semantics')
    import app
    from persistence import atomic_write_json

    work = ROOT / '.cache' / '状态写入验收' / f'{time.time_ns()}_{uuid.uuid4().hex[:8]}'
    work.mkdir(parents=True)
    report = dict(fixture_only=True, real_model_generation=False,
                  directory=str(work), checks=[], success=False)
    original_command = app.worker_command
    original_terminate = app.terminate_tree
    store = None
    held = None
    timer = None

    def check(name, condition, **evidence):
        report['checks'].append(dict(name=name, passed=bool(condition), **evidence))
        if not condition:
            raise AssertionError(name)

    try:
        path = work / 'short-lock.json'
        atomic_write_json(path, {'version': 'before'})
        held = NoDeleteShare(path)
        start = time.monotonic()
        try:
            atomic_write_json(path, {'version': 'must-not-replace'})
        except OSError as exc:
            check('real Windows sharing lock prevents replace',
                  getattr(exc, 'winerror', None) in (5, 32, 33),
                  winerror=getattr(exc, 'winerror', None), elapsed=round(time.monotonic()-start, 3))
        else:
            raise AssertionError('Handle did not prevent replacement')
        check('failed replacement preserves existing JSON', read_json(path) == {'version': 'before'})
        check('failed replacement removes unique temp files', not list(work.glob('.short-lock.json.*.tmp')))
        timer = threading.Timer(.12, held.close)
        timer.start()
        start = time.monotonic()
        atomic_write_json(path, {'version': 'after'})
        timer.join()
        held = None
        check('short lock is retried successfully', read_json(path) == {'version': 'after'},
              elapsed=round(time.monotonic()-start, 3))

        data = work / 'fixture-data'
        store = app.JobStore(data)
        app.worker_command = lambda *args: [sys.executable, '-u', str(Path(__file__).resolve()),
                                             '--fixture-worker', *map(str, args)]
        terminated = []

        def observed_terminate(proc):
            terminated.append(getattr(proc, 'pid', None))
            return original_terminate(proc)

        app.terminate_tree = observed_terminate
        public = store.create(dict(name='真实文件锁验收 fixture，非模型生成',
            bbox=[118.77, 32.02, 118.78, 32.03], method='withtopo'))
        job_id = public['id']
        folder = store.job_directory(job_id)
        metadata = folder / 'app_job.json'
        before = read_json(metadata)
        held = NoDeleteShare(metadata)
        (folder / 'fixture-go').touch()
        store.threads[job_id].join(timeout=25)
        check('real fixture subprocess completes while metadata remains locked',
            not store.threads[job_id].is_alive() and store.jobs[job_id]['status'] == 'complete',
            status=store.jobs[job_id]['status'], worker=read_json(folder / 'fixture-exited.json'))
        check('metadata errors never terminate the fixture worker', not terminated,
              termination_calls=terminated)
        job = store.jobs[job_id]
        check('completion and progress remain available in memory',
              job['progress'] == 100 and job['stats'].get('fixture_only') is True)
        check('locked primary snapshot stays unchanged', read_json(metadata) == before,
              primary_revision=before['_state_revision'])
        recovery = read_json(folder / 'app_job.recovery.json')
        check('recovery snapshot contains the newer completed state',
              recovery['status'] == 'complete' and recovery['progress'] == 100
              and recovery['_state_revision'] > before['_state_revision'],
              recovery_revision=recovery['_state_revision'])
        check('warning and pending retry are exposed while locked',
              bool(job.get('persistence_warning')) and job_id in store.pending_saves)
        restarted = app.JobStore(data)
        restored = restarted.jobs[job_id]
        check('fresh JobStore restores completion from recovery despite primary lock',
              restored['status'] == 'complete' and restored['progress'] == 100
              and restored['stats'].get('fixture_only') is True,
              revision=restored['_state_revision'])
        held.close()
        held = None
        restarted.flush_pending(force=True)
        saved = read_json(metadata)
        check('unlock and forced flush persist completion',
              saved['status'] == 'complete' and saved['progress'] == 100
              and saved['_state_revision'] > recovery['_state_revision'],
              primary_revision=saved['_state_revision'])
        check('successful flush clears pending retry, warning, and recovery file',
              job_id not in restarted.pending_saves
              and 'persistence_warning' not in restored
              and not (folder / 'app_job.recovery.json').exists())
        reopened = app.JobStore(data)
        check('subsequent restart retains completed fixture without recovery file',
              reopened.jobs[job_id]['status'] == 'complete')
        check('no temporary metadata files remain', not list(folder.glob('.*.tmp')))
        report['job_id'] = job_id
        report['success'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        if timer:
            timer.join(timeout=2)
        if held:
            held.close()
        app.worker_command = original_command
        app.terminate_tree = original_terminate
        if store:
            for proc in list(store.processes.values()):
                original_terminate(proc)
        result = work / 'result.json'
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
        print(f'RESULT_PATH={result}', flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--fixture-worker':
        raise SystemExit(fixture_worker(*sys.argv[2:]))
    raise SystemExit(validate())
