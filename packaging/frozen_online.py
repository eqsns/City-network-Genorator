"""Verify all remote data download paths in the frozen executable."""
import json,os,subprocess,time,sys
from pathlib import Path
import yaml
sys.stdout.reconfigure(encoding='utf-8')
root=Path(__file__).resolve().parents[1]
exe=root/'dist'/'SWMManywhereStudio'/'SWMManywhereStudio.exe'
work=root/'.cache'/'frozen-online'
work.mkdir(parents=True,exist_ok=True)
project='online_'+str(time.time_ns())
config=dict(base_dir=str(work),project=project,bbox=[118.774,32.023,118.787,32.034],run_model=False,
            parameter_overrides={'outfall_derivation':{'method':'withtopo'}},
            address_overrides={'whiteboxtools_binaries_zip':str(exe.parent/'_internal'/'native'/'whitebox_tools.zip')})
path=work/'config.yml'
if '--resume' not in sys.argv or not path.exists():
    path.write_text(yaml.safe_dump(config),encoding='utf-8')
events=work/'events.jsonl';events.write_text('')
env=os.environ.copy();env['PATH']=os.path.join(os.environ.get('SYSTEMROOT',r'C:\Windows'),'System32')
env.pop('PYTHONPATH',None);env.pop('VIRTUAL_ENV',None);env['NUMBA_CACHE_DIR']=str(work/'numba-cache')
proc=subprocess.Popen([str(exe),'--worker',str(path),str(events),'frozen-online','便携版联网验收'],cwd=work,env=env)
print('FROZEN_ONLINE_PID',proc.pid,flush=True)
offset=0
while proc.poll() is None:
    lines=events.read_text(encoding='utf-8').splitlines()
    for line in lines[offset:]:print(line,flush=True)
    offset=len(lines);time.sleep(2)
print('FROZEN_ONLINE_EXIT',proc.returncode,flush=True)
print(events.read_text(encoding='utf-8').splitlines()[-1],flush=True)
raise SystemExit(proc.returncode)
