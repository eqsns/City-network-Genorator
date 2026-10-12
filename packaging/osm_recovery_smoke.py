"""Actual generation in an isolated workspace, without adding user history."""
import json
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(root))
from app import ACTIVE, JobStore

work = root / '.cache' / 'osm-recovery-20261006'
store = JobStore(work)
bbox = [109.443226, 36.569573, 109.531631, 36.617569]
job = store.create(dict(name='延安 OSM 修复实测', bbox=bbox))
(work/'latest-job.txt').write_text(job['id'], encoding='utf-8')
print(json.dumps(dict(job=job['id'], bbox=bbox, data=str(work)), ensure_ascii=False), flush=True)
last = None
deadline = time.monotonic() + 1200
try:
    while True:
        with store.lock:
            result = store.public(store.jobs[job['id']])
        key = (result['status'], result['progress'], result['stage'])
        if key != last:
            print(json.dumps(result, ensure_ascii=False), flush=True)
            last = key
        if result['status'] not in ACTIVE:
            break
        if time.monotonic() >= deadline:
            store.cancel(job['id'])
            raise RuntimeError('联网验收达到20分钟总上限，已取消')
        time.sleep(1)
finally:
    store.close()
(work/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
raise SystemExit(0 if result['status'] == 'complete' else 1)
