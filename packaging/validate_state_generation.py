"""Run a real packaged model under a persistent Windows metadata lock.

The application supervisor runs from source; the calculation runs from the
specified portable EXE. All four completed inputs are copied to isolated QA
storage. This is cached generation, not a new network availability test.
"""
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
from validate_state_lock import NoDeleteShare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--input-cache', type=Path, required=True)
    parser.add_argument('--bbox', type=float, nargs=4, required=True)
    args = parser.parse_args()
    exe = args.exe.resolve()
    key = hashlib.sha256(json.dumps(args.bbox).encode()).hexdigest()[:20]
    assert args.input_cache.name == key, 'Input cache must match the exact bbox'
    work = ROOT / '.cache' / '状态写入真实生成验收' / str(time.time_ns())
    data = work / 'data'
    cache = data / 'cache' / key
    cache.mkdir(parents=True)
    for filename in app.DOWNLOADS.values():
        source = args.input_cache / filename
        assert source.is_file() and source.stat().st_size > 0
        shutil.copy2(source, cache / filename)
    app.ASSETS = exe.parent / '_internal'
    app.worker_command = lambda *values: [str(exe), '--worker', *map(str, values)]
    os.environ['PATH'] = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32')
    os.environ.pop('PYTHONPATH', None)
    os.environ.pop('VIRTUAL_ENV', None)
    store = app.JobStore(data)
    started = time.monotonic()
    job_id = store.create(dict(name='状态文件持续占用 · 真实管网验收', bbox=args.bbox))['id']
    folder = store.job_directory(job_id)
    lock = NoDeleteShare(folder / 'app_job.json')
    print(json.dumps({'work': str(work), 'job_id': job_id}, ensure_ascii=False), flush=True)
    last = None
    warning_seen = False
    try:
        while store.threads[job_id].is_alive():
            with store.lock:
                job = store.public(store.jobs[job_id])
            warning_seen |= bool(job.get('persistence_warning'))
            progress = (job['status'], job['progress'], job.get('stage'))
            if progress != last:
                print(json.dumps(job, ensure_ascii=False), flush=True)
                last = progress
            if time.monotonic() - started > 600:
                raise TimeoutError('Cached model validation exceeded 10 minutes')
            time.sleep(.5)
        job = store.jobs[job_id]
        assert warning_seen, 'The test did not exercise a state-save failure'
        assert job['status'] == 'complete' and job['progress'] == 100, job
        recovery = folder / 'app_job.recovery.json'
        assert json.loads(recovery.read_text(encoding='utf-8'))['status'] == 'complete'
        # Recreate the supervisor with the canonical file still locked. The
        # completed snapshot must survive even before canonical healing works.
        restarted = app.JobStore(data)
        assert restarted.jobs[job_id]['status'] == 'complete'
        lock.close()
        restarted.flush_pending(force=True)
        assert not restarted.jobs[job_id].get('persistence_warning')
        assert json.loads((folder / 'app_job.json').read_text(encoding='utf-8'))['status'] == 'complete'
        assert not recovery.exists()
        events = [json.loads(line) for line in (folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        assert not any(event.get('type') == 'error' for event in events)
        assert not any('正在连接 OSM 服务' in event.get('message', '') for event in events)
        result = dict(seconds=round(time.monotonic()-started, 2), bbox=args.bbox,
                      stats=job['stats'], model=str(store.model_dir(job)), exe=str(exe),
                      exe_sha256=hashlib.file_digest(exe.open('rb'), 'sha256').hexdigest(),
                      status='complete', cached_inputs=list(app.DOWNLOADS),
                      fresh_network_test=False, source_supervisor=True,
                      persistent_real_windows_lock=True, recovered_after_restart=True)
        (work / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        lock.close()
        store.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
