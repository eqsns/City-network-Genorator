"""Run the packaged worker with an isolated PATH and relocate its SWMM export."""
import sys,json,os,subprocess,zipfile,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bootstrap
import yaml
root=bootstrap.ROOT
sys.stdout.reconfigure(encoding='utf-8')
original=root/'.cache'/'release-stage'/'SWMManywhereStudio'
relocated=root/'.cache'/'软件搬迁 空格'/'SWMManywhereStudio'
shutil.copytree(original,relocated,dirs_exist_ok=True)
exe=relocated/'SWMManywhereStudio.exe'
work=root/'.cache'/'便携 验收'
work.mkdir(parents=True,exist_ok=True)
source=root/'data'/'runs'/'20260929_134536_c1c392'/'bbox_1'/'download'
project='frozen_run_'+str(__import__('time').time_ns())
config=dict(base_dir=str(work),project=project,bbox=[118.774,32.023,118.787,32.034],run_model=False,
            parameter_overrides={'outfall_derivation':{'method':'withtopo'}},
            address_overrides={k:str(source/v) for k,v in dict(elevation='elevation.tif',building='building.geoparquet',street='street.json',river='river.json').items()})
config['address_overrides']['whiteboxtools_binaries_zip']=str(exe.parent/'_internal'/'native'/'whitebox_tools.zip')
config_path=work/'config.yml';config_path.write_text(yaml.safe_dump(config),encoding='utf-8')
events=work/'events.jsonl';events.write_text('')
env=os.environ.copy();env['PATH']=os.path.join(os.environ.get('SYSTEMROOT',r'C:\Windows'),'System32')
env.pop('PYTHONPATH',None);env.pop('VIRTUAL_ENV',None);env['NUMBA_CACHE_DIR']=str(work/'numba-cache')
proc=subprocess.Popen([str(exe),'--worker',str(config_path),str(events),'frozen-test','便携版实际生成'],cwd=work,env=env)
print('FROZEN_PID',proc.pid,flush=True)
code=proc.wait(timeout=600)
print('FROZEN_EXIT',code,flush=True)
print(events.read_text(encoding='utf-8'),flush=True)
if code:sys.exit(code)
model=work/project/'bbox_1'/'model_1'
target=work/'成果搬迁 中文'
target.mkdir(exist_ok=True)
with zipfile.ZipFile(model/'SWMM-model.zip') as archive:archive.extractall(target)
inp=next(target.glob('*.inp'))
assert '"storm.dat"' in inp.read_text(encoding='utf-8')
assert (target/'storm.dat').exists()
print('RELOCATED_EXPORT_OK',flush=True)
