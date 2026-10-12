"""Verify the actual portable worker with fresh OSM requests and full generation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
import yaml

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.stdout.reconfigure(encoding='utf-8')
from app import JobStore, terminate_tree

parser = argparse.ArgumentParser()
parser.add_argument('--reuse-osm-from', type=Path, help='Optional prior QA cache directory for an offline regression')
parser.add_argument('--allow-missing-osm', action='store_true', help='Download missing queries when reusing partial OSM cache')
parser.add_argument('--bbox', type=float, nargs=4, help='Optional west south east north test range')
parser.add_argument('--input-cache', type=Path, help='Completed application base-data cache matching the bbox hash')
arguments = parser.parse_args()

bbox = arguments.bbox or [109.443226, 36.569573, 109.531631, 36.617569]
previous = JobStore(root/'.cache'/'osm-recovery-20261006')
inputs = previous.download_sources(bbox)
if arguments.input_cache:
    assert arguments.input_cache.name == hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
    for key, filename in (('elevation', 'elevation.tif'), ('building', 'building.geoparquet')):
        source = arguments.input_cache.resolve()/filename
        assert source.is_file() and source.stat().st_size > 4
        inputs[key] = str(source)
exe = root/'dist'/'SWMManywhereStudio'/'SWMManywhereStudio.exe'
run_id = str(time.time_ns())
work = root/'.cache'/'OSM 修复 便携验收'/run_id
work.mkdir(parents=True, exist_ok=True)
if arguments.reuse_osm_from:
    shutil.copytree(arguments.reuse_osm_from, work/'cache')
project = 'yanan_'+run_id
config = dict(base_dir=str(work), project=project, bbox=bbox, run_model=False,
              parameter_overrides={'outfall_derivation': {'method': 'withtopo'}},
              address_overrides={key: inputs[key] for key in ('elevation','building') if key in inputs})
config['address_overrides']['whiteboxtools_binaries_zip'] = str(exe.parent/'_internal'/'native'/'whitebox_tools.zip')
config_path = work/'config.yml'
config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
events = work/'events.jsonl'
events.write_text('', encoding='utf-8')
environment = os.environ.copy()
environment['PATH'] = os.path.join(os.environ.get('SYSTEMROOT',r'C:\Windows'),'System32')
environment.pop('PYTHONPATH', None)
environment.pop('VIRTUAL_ENV', None)
environment.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', NUMBA_CACHE_DIR=str(work/'numba-cache'))
started = time.monotonic()
process = subprocess.Popen([str(exe), '--worker', str(config_path), str(events), project, 'OSM 原范围修复便携验收'],
                           cwd=work, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
offset = 0
try:
    while True:
        records = events.read_text(encoding='utf-8').splitlines()
        for record in records[offset:]:
            print(record, flush=True)
        offset = len(records)
        if process.poll() is not None:
            break
        if time.monotonic()-started > 900:
            raise TimeoutError('便携验收达到15分钟上限')
        time.sleep(1)
finally:
    terminate_tree(process)
records = [json.loads(line) for line in events.read_text(encoding='utf-8').splitlines()]
result = next((record for record in reversed(records) if record['type']=='result'), None)
model = work/project/'bbox_1'/'model_1'
if process.returncode != 0 or not result or not all((model/name).is_file() for name in
        ('model_1.inp', 'network.geojson', 'network.png', 'SWMM-model.zip')):
    raise RuntimeError(f'便携验收失败，退出代码 {process.returncode}；见 {work}')
summary = dict(seconds=round(time.monotonic()-started,2), stats=result['stats'],
               model=str(model), inputs_reused=[key for key in ('elevation','building') if key in inputs],
               fresh_osm=not bool(arguments.reuse_osm_from), bbox=bbox,
               osm_downloads=sum('OSM 数据下载完成' in record.get('message','') for record in records),
               osm_cache_hits=sum('缓存的 OSM 查询结果' in record.get('message','') for record in records))
if arguments.reuse_osm_from:
    assert any('缓存的 OSM 查询结果' in record.get('message','') for record in records)
    if not arguments.allow_missing_osm:
        assert not any('正在连接 OSM 服务' in record.get('message','') for record in records)
else:
    assert not any('缓存的 OSM 查询结果' in record.get('message','') for record in records)
    assert any('OSM 数据下载完成' in record.get('message','') for record in records)
(work/'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False), flush=True)
