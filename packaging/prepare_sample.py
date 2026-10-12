"""Copy (never modify) original Nanjing results into the redistributable sample."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bootstrap
import shutil
import json
import hashlib
from artifacts import prepare_artifacts
from app import SAMPLE_BBOX

root=bootstrap.ROOT
source=root/'output'/'nanjing_core'/'bbox_1'/'model_4'
target=root/'sample'
target.mkdir(exist_ok=True)
for name in ['model_4.inp','storm.dat','edges.geoparquet','nodes.geoparquet','subcatchments.geoparquet']:
    shutil.copy2(source/name,target/name)
shutil.copy2(source/'nanjing_network.png',target/'network.png')
print(prepare_artifacts(target,'南京主城区核心范围',draw_png=False))
shutil.copytree(target,root/'data'/'sample',dirs_exist_ok=True)
cache=root/'data'/'cache'/hashlib.sha256(json.dumps(SAMPLE_BBOX).encode()).hexdigest()[:20]
cache.mkdir(parents=True,exist_ok=True)
download=source.parent/'download'
for name in ['elevation.tif','building.geoparquet','street.json','river.json']:
    shutil.copy2(download/name,cache/name)
