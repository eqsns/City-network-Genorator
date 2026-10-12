"""Exercise the frozen worker's real pause/resume and isolated cache cleanup."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def wait_for(store, job_id, predicate, timeout=120):
    until = time.monotonic() + timeout
    last = None
    while time.monotonic() < until:
        with store.lock:
            job = store.public(store.jobs[job_id])
        signature = (job['status'], job['progress'], job['stage'])
        if signature != last:
            print(json.dumps(job, ensure_ascii=False), flush=True)
            last = signature
        if predicate(job):
            return job
        if job['status'] not in app.ACTIVE:
            raise AssertionError(job)
        time.sleep(.1)
    raise TimeoutError('Timed out waiting for real worker')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--corrupt-shared-cache', action='store_true',
                        help='Check real generation falls back to valid historic inputs')
    args = parser.parse_args()
    exe = args.exe.resolve()
    bbox = [118.707962, 32.218133, 118.784695, 32.275229]
    source = ROOT / '.cache/状态写入真实生成验收/1791508581992797400/data/cache/19f7c2fe84434d792bf0'
    work = ROOT / '.cache/暂停恢复与缓存验收' / str(time.time_ns())
    data = work / 'data'
    key = hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
    assert source.name == key
    cache = data / 'cache' / key
    cache.mkdir(parents=True)
    inputs = {}
    for name in app.DOWNLOADS.values():
        shutil.copy2(source / name, cache / name)
        inputs[name] = digest(cache / name)
    historic_id = None
    if args.corrupt_shared_cache:
        historic_id = '20261010_151916_25b79b'
        historic = data / 'runs' / historic_id
        shutil.copytree(ROOT / '.cache/ui-pause-cache-20261010/data/runs' / historic_id, historic)
        downloaded = historic / 'bbox_1/download'
        downloaded.mkdir(exist_ok=True)
        for name in app.DOWNLOADS.values():
            shutil.copy2(source / name, downloaded / name)
            (cache / name).write_bytes(b'{"directed":' if name.endswith('.json') else b'')
    app.ASSETS = exe.parent / '_internal'
    app.worker_command = lambda *args: [str(exe), '--worker', *map(str, args)]
    os.environ['PATH'] = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32')
    for name in ('PYTHONPATH', 'VIRTUAL_ENV'):
        os.environ.pop(name, None)
    store = app.JobStore(data)
    started = time.monotonic()
    result = dict(work=str(work), exe=str(exe), exe_sha256=digest(exe), bbox=bbox,
                  inputs_sha256=inputs, fresh_network_test=False, source_supervisor=True,
                  corrupt_shared_cache_test=args.corrupt_shared_cache)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    try:
        job_id = store.create(dict(name='暂停恢复真实管网验收', bbox=bbox))['id']
        result['job_id'] = job_id
        if historic_id:
            import yaml
            config = yaml.safe_load((store.job_directory(job_id) / 'config.yml').read_text(encoding='utf-8'))
            assert all(Path(config['address_overrides'][key]).is_relative_to(data / 'runs' / historic_id)
                       for key in app.DOWNLOADS)
            result['invalid_shared_inputs_fell_back_to_history'] = True
        store.pause(job_id)
        wait_for(store, job_id, lambda job: job['status'] == 'paused')
        pid = store.processes[job_id].pid
        snapshots = []
        for cycle in range(2):
            state = store.public(store.jobs[job_id])
            event_file = store.job_directory(job_id) / 'events.jsonl'
            event_digest = digest(event_file)
            time.sleep(3)
            frozen = store.public(store.jobs[job_id])
            assert frozen['status'] == 'paused'
            assert frozen['progress'] == state['progress'] and frozen['elapsed'] == state['elapsed']
            assert digest(event_file) == event_digest
            assert store.processes[job_id].pid == pid and store.processes[job_id].poll() is None
            try:
                store.clear_cache({'generation': True})
            except RuntimeError:
                pass
            else:
                raise AssertionError('Paused task must prevent cache clearing')
            snapshots.append(dict(cycle=cycle+1, pid=pid, progress=frozen['progress'],
                                  elapsed=frozen['elapsed'], event_file_unchanged=True))
            store.resume(job_id)
            wait_for(store, job_id, lambda job: job['status'] == 'running')
            if cycle == 0:
                wait_for(store, job_id, lambda job: job.get('phase') == 'graph' and job['progress'] >= 45)
                store.pause(job_id)
                wait_for(store, job_id, lambda job: job['status'] == 'paused')
        complete = wait_for(store, job_id, lambda job: job['status'] == 'complete', timeout=600)
        store.threads[job_id].join(timeout=10)
        assert not store.threads[job_id].is_alive()
        assert complete['stats']['street_conduits'] == 3743
        result.update(status=complete['status'], stats=complete['stats'], pauses=snapshots,
                      model=str(store.model_dir(store.jobs[job_id])), paused_seconds=complete['paused_seconds'],
                      generation_seconds=round(time.monotonic()-started, 2))
        events = [json.loads(line) for line in event_file.read_text(encoding='utf-8').splitlines()]
        assert not any(record['type'] == 'error' for record in events)
        assert not any('正在连接 OSM 服务' in record.get('message','') for record in events)
        cancelled_id = store.create(dict(name='暂停后取消验收', bbox=bbox))['id']
        store.pause(cancelled_id)
        wait_for(store, cancelled_id, lambda job: job['status'] == 'paused')
        proc = store.processes[cancelled_id]
        store.cancel(cancelled_id)
        wait_for(store, cancelled_id, lambda job: job['status'] == 'cancelled')
        store.threads[cancelled_id].join(timeout=10)
        assert proc.poll() is not None and store.jobs[cancelled_id]['progress'] < 100
        result['cancel_while_paused'] = True
        if historic_id:
            store.delete(historic_id, clear_cache=False)
            assert all(digest(cache / name) == expected for name, expected in inputs.items())
            result['preserve_on_delete_repaired_shared_inputs'] = True
        model = store.model_dir(store.jobs[job_id])
        hashes = {item.name:digest(item) for item in model.iterdir() if item.is_file()}
        (data / 'map-view.json').write_text('{"test":"preserve"}', encoding='utf-8')
        original_view = (data / 'map-view.json').read_bytes()
        result['cache_before'] = store.cache_info()
        result['cache_cleared'] = store.clear_cache({'generation': True})
        assert store.cache_info()['generation']['bytes'] == 0
        assert not store.download_sources(bbox)
        assert hashes == {item.name:digest(item) for item in model.iterdir() if item.is_file()}
        assert (data / 'map-view.json').read_bytes() == original_view
        assert app.JobStore(data).jobs[job_id]['status'] == 'complete'
        result['cache_clear_preserved_models_history_and_view'] = True
        result['passed'] = True
    except BaseException as exc:
        result.update(passed=False, error=repr(exc))
        raise
    finally:
        store.close()
        result['total_seconds'] = round(time.monotonic()-started, 2)
        (work / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
