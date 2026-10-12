"""Re-run an exact failed area with a portable worker in isolated QA storage."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from stress_1000 import memory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-job', required=True, type=Path)
    parser.add_argument('--exe', required=True, type=Path)
    args = parser.parse_args()
    source, exe = args.source_job.resolve(), args.exe.resolve()
    original = json.loads((source / 'app_job.json').read_text(encoding='utf-8'))
    assert original.get('phase') in {'graph', 'write', 'render', 'verify'} or original['status'] == 'complete', 'Inputs must have completed'
    bbox = original['bbox']
    work = ROOT / '.cache' / '原范围几何修复验收' / str(time.time_ns())
    data = work / 'data'
    cache = data / 'cache' / hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
    cache.mkdir(parents=True)
    inputs = {}
    for key, filename in app.DOWNLOADS.items():
        path = source / 'bbox_1' / 'download' / filename
        assert path.is_file() and path.stat().st_size > 0
        shutil.copy2(path, cache / filename)
        with path.open('rb') as stream:
            inputs[key] = dict(source=str(path), sha256=hashlib.file_digest(stream, 'sha256').hexdigest())
    app.ASSETS = exe.parent / '_internal'
    app.worker_command = lambda *values: [str(exe), '--worker', *map(str, values)]
    os.environ['PATH'] = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32')
    os.environ.pop('PYTHONPATH', None)
    os.environ.pop('VIRTUAL_ENV', None)
    store = app.JobStore(data)
    started = time.monotonic()
    job_id = store.create(dict(name='原范围 · 几何修复验证', bbox=bbox, method=original.get('method', 'withtopo')))['id']
    w, s, e, n = bbox
    result = dict(source_job=original['id'], bbox=bbox, input_files=inputs, work=str(work),
                  app_area_km2=(e-w)*111.32*math.cos(math.radians((s+n)/2))*(n-s)*111.32,
                  job_id=job_id, exe=str(exe), source_supervisor=True, fresh_network_test=False)
    with exe.open('rb') as stream:
        result['exe_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    (work / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    last, low_since = None, None
    peak_working, peak_private = 0, 0
    try:
        with (work / 'resources.jsonl').open('w', encoding='utf-8') as resources:
            while store.threads[job_id].is_alive():
                with store.lock:
                    state = store.public(store.jobs[job_id])
                    proc = store.processes.get(job_id)
                signature = (state['status'], state['progress'], state['stage'])
                if signature != last:
                    print(json.dumps(state, ensure_ascii=False), flush=True)
                    last = signature
                sample = memory(proc)
                sample['seconds'] = round(time.monotonic()-started, 2)
                resources.write(json.dumps(sample)+'\n')
                resources.flush()
                peak_working = max(peak_working, sample.get('peak_working_set', 0))
                peak_private = max(peak_private, sample.get('private_bytes', 0))
                if sample['seconds'] > 3600:
                    raise TimeoutError('Cached exact-area test exceeded 60 minutes')
                if sample['available_ram'] < max(2*1024**3, sample['total_ram']*.05):
                    low_since = low_since or time.monotonic()
                    if time.monotonic()-low_since > 15:
                        raise MemoryError('Low available memory persisted for 15 seconds')
                else:
                    low_since = None
                time.sleep(1)
    except BaseException as exc:
        result['harness_error'] = repr(exc)
    finally:
        store.close()
        job = store.jobs[job_id]
        events = store.job_directory(job_id) / 'events.jsonl'
        records = [json.loads(line) for line in events.read_text(encoding='utf-8').splitlines()]
        connections = sum('正在连接 OSM 服务' in record.get('message', '') for record in records)
        result.update(status=job['status'], error=job.get('error'), stats=job.get('stats'),
                      seconds=round(time.monotonic()-started, 2), model=str(store.model_dir(job)),
                      peak_worker_working_set=peak_working, peak_worker_private_bytes=peak_private,
                      osm_connections=connections)
        result['passed'] = job['status'] == 'complete' and not connections and not result.get('harness_error')
        (work / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
