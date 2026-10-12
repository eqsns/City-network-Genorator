"""Real online generation acceptance run. Leaves inputs, events and logs on disk."""
import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import JobStore,ACTIVE
from bootstrap import ROOT
store=JobStore(ROOT/'data')
job=store.create({'name':'南京小范围联网验收','bbox':[118.774,32.023,118.787,32.034]})
print('JOB',job['id'],flush=True)
last=None
while True:
    state=store.public(store.jobs[job['id']])
    signature=(state['status'],state['progress'],state['stage'])
    if signature!=last:
        print(json.dumps(state,ensure_ascii=False),flush=True);last=signature
    if state['status'] not in ACTIVE:
        break
    time.sleep(2)
store.close()
sys.exit(0 if state['status']=='complete' else 1)
