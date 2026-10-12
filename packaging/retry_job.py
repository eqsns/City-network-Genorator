"""Reproduce a user's failed range using the packaged calculation worker.

Writes a new history entry and leaves the failed task intact. Requires the
desktop to be closed so two managers cannot access the same data directory.
"""
import json
import os
import sys
import time
from pathlib import Path
from filelock import FileLock

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
import app

sys.stdout.reconfigure(encoding='utf-8')
exe=root/'dist'/'SWMManywhereStudio'/'SWMManywhereStudio.exe'
data=exe.parent/'data'
app.ASSETS=exe.parent/'_internal'
app.worker_command=lambda *args:[str(exe),'--worker',*map(str,args)]
os.environ['PATH']=os.path.join(os.environ.get('SYSTEMROOT',r'C:\Windows'),'System32')
os.environ.pop('PYTHONPATH',None)
os.environ.pop('VIRTUAL_ENV',None)
with FileLock(str(data/'.studio.lock'),timeout=0):
    store=app.JobStore(data)
    original=store.jobs[sys.argv[1]]
    job=store.create(dict(name='苏州范围 · 联网修复验收',bbox=original['bbox'],method=original.get('method','withtopo')))
    print('JOB',job['id'],flush=True)
    last=None
    while True:
        current=store.public(store.jobs[job['id']])
        progress=(current['status'],current['progress'],current['stage'])
        if progress!=last:
            print(json.dumps(current,ensure_ascii=False),flush=True)
            last=progress
        if current['status'] not in app.ACTIVE:
            break
        time.sleep(2)
    store.close()
    raise SystemExit(0 if current['status']=='complete' else 1)
