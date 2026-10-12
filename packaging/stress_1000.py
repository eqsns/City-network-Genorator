"""Test the shipped EXE in isolated storage; retain download and failure evidence."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml
from pyproj import Geod

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import terminate_tree, validate


class MemoryStatus(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in
        ('total', 'available', 'total_page', 'available_page', 'total_virtual', 'available_virtual', 'extended')]


class ProcessMemory(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in
        ('peak_working_set', 'working_set', 'peak_paged', 'paged', 'peak_nonpaged',
         'nonpaged', 'pagefile', 'peak_pagefile', 'private')]


kernel = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi', use_last_error=True)
kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemory), wintypes.DWORD]


def memory(process=None):
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    sample = {'total_ram': status.total, 'available_ram': status.available}
    if process and process.poll() is None:
        usage = ProcessMemory()
        usage.cb = ctypes.sizeof(usage)
        if not psapi.GetProcessMemoryInfo(int(process._handle), ctypes.byref(usage), usage.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        sample.update(working_set=usage.working_set, peak_working_set=usage.peak_working_set,
                      private_bytes=usage.private)
    return sample


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', type=Path, help='Reuse only completed downloads from this exact-bbox QA run')
    parser.add_argument('--fresh-osm', action='store_true', help='Reuse completed base data but download all OSM queries again')
    parser.add_argument('--exe', type=Path, help='Portable EXE to test, defaults to delivered version')
    parser.add_argument('--bbox', nargs=4, type=float, metavar=('WEST', 'SOUTH', 'EAST', 'NORTH'),
                        help='Test another region instead of the default Nanjing extent')
    parser.add_argument('--name', default='南京近1000平方公里压力验收')
    parser.add_argument('--require-cold', action='store_true',
                        help='Require >500 km2, empty local inputs/cache, and zero OSM cache hits')
    arguments = parser.parse_args()
    if arguments.require_cold and (arguments.resume or arguments.fresh_osm):
        parser.error('--require-cold cannot reuse any earlier data')
    bbox = arguments.bbox or [118.62243177908188, 31.907971633923268, 118.95756822091813, 32.19202836607673]
    validate({'name': arguments.name, 'bbox': bbox})
    w, s, e, n = bbox
    app_area = (e-w)*111.32*math.cos(math.radians((s+n)/2))*(n-s)*111.32
    geod_area = abs(Geod(ellps='WGS84').polygon_area_perimeter([w,e,e,w], [s,s,n,n])[0])/1e6
    if arguments.require_cold and min(app_area, geod_area) <= 500:
        parser.error('Cold large-area verification requires more than 500 km2')
    run_id = str(time.time_ns())
    work = ROOT/'.cache'/('新区域全量下载验收' if arguments.require_cold else '1000 平方公里压力验收')/run_id
    work.mkdir(parents=True)
    initial_entries = list(work.iterdir())
    assert not initial_entries, 'Verification directory must start empty'
    exe = (arguments.exe or ROOT/'dist'/'SWMManywhereStudio'/'SWMManywhereStudio.exe').resolve()
    project = ('cold_area_' if arguments.bbox else 'nanjing_1000_')+run_id
    config = dict(base_dir=str(work), project=project, bbox=bbox, run_model=False,
                  parameter_overrides={'outfall_derivation': {'method': 'withtopo'}},
                  address_overrides={'whiteboxtools_binaries_zip': str(exe.parent/'_internal'/'native'/'whitebox_tools.zip')})
    inputs_reused = []
    if arguments.resume:
        previous = arguments.resume.resolve()
        manifest = json.loads((previous/'manifest.json').read_text(encoding='utf-8'))
        assert manifest['bbox'] == bbox, 'Cannot reuse inputs from a different extent'
        records = [json.loads(line) for line in (previous/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        # Entering street proves both preceding input stages completed normally.
        assert any(record.get('phase') == 'street' for record in records)
        previous_config = yaml.safe_load((previous/'config.yml').read_text(encoding='utf-8'))
        download = previous/previous_config['project']/'bbox_1'/'download'
        for key, filename in (('elevation','elevation.tif'), ('building','building.geoparquet')):
            source = download/filename
            assert source.is_file() and source.stat().st_size > 4
            config['address_overrides'][key] = str(source)
            inputs_reused.append(key)
        if not arguments.fresh_osm and (previous/'cache').is_dir():
            shutil.copytree(previous/'cache', work/'cache')
            inputs_reused.append('complete_osm_query_cache')
    config_path = work/'config.yml'
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
    events = work/'events.jsonl'
    events.write_text('', encoding='utf-8')
    environment = os.environ.copy()
    environment['PATH'] = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32')
    for key in ('PYTHONPATH', 'VIRTUAL_ENV'):
        environment.pop(key, None)
    environment.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', NUMBA_CACHE_DIR=str(work/'numba-cache'))
    summary = dict(name=arguments.name, bbox=bbox, app_area_km2=app_area, geodesic_area_km2=geod_area,
                   inputs_reused=inputs_reused, work=str(work), executable=str(exe), initial_memory=memory(),
                   require_cold=arguments.require_cold, initial_work_entries=[str(p) for p in initial_entries],
                   address_overrides=config['address_overrides'],
                   memory_scope='worker process only; external native child peak not included')
    with exe.open('rb') as executable:
        summary['executable_sha256'] = hashlib.file_digest(executable, 'sha256').hexdigest()
    summary['network_modules_sha256'] = {
        name: hashlib.sha256((exe.parent/'_internal'/'vendor'/'swmmanywhere-src'/'swmmanywhere'/name).read_bytes()).hexdigest()
        for name in ('osm_network.py', 'osm_queries.py', 'osm_cache.py')
        if (exe.parent/'_internal'/'vendor'/'swmmanywhere-src'/'swmmanywhere'/name).exists()}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    (work/'manifest.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    started = time.monotonic()
    process = None
    offset = 0
    peak_working = peak_private = 0
    minimum_available = summary['initial_memory']['available_ram']
    low_since = None
    try:
        with (work/'worker.log').open('w', encoding='utf-8') as log, (work/'resources.jsonl').open('w', encoding='utf-8') as resources:
            process = subprocess.Popen([str(exe), '--worker', str(config_path), str(events), project, arguments.name],
                                       cwd=work, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            print('WORKER_PID', process.pid, flush=True)
            while True:
                records = events.read_text(encoding='utf-8').splitlines()
                for record in records[offset:]:
                    print(record, flush=True)
                offset = len(records)
                sample = memory(process)
                sample['seconds'] = round(time.monotonic()-started, 2)
                resources.write(json.dumps(sample)+'\n')
                resources.flush()
                peak_working = max(peak_working, sample.get('peak_working_set', 0))
                peak_private = max(peak_private, sample.get('private_bytes', 0))
                minimum_available = min(minimum_available, sample['available_ram'])
                if process.poll() is not None:
                    break
                if sample['seconds'] > 5400:
                    raise TimeoutError('压力验收达到90分钟上限')
                if sample['available_ram'] < max(2*1024**3, sample['total_ram']*0.05):
                    low_since = low_since or time.monotonic()
                    if time.monotonic()-low_since > 15:
                        raise MemoryError('持续15秒可用内存低于2GiB或总内存5%，终止本次验收')
                else:
                    low_since = None
                time.sleep(2)
    except BaseException as exc:
        summary['harness_error'] = f'{type(exc).__name__}: {exc}'
    finally:
        terminate_tree(process)
        records = [json.loads(line) for line in events.read_text(encoding='utf-8').splitlines() if line.strip()]
        result = next((record for record in reversed(records) if record['type']=='result'), None)
        summary.update(seconds=round(time.monotonic()-started,2), exit_code=process.returncode if process else None,
                       worker_peak_working_set=peak_working, worker_peak_private_bytes=peak_private,
                       minimum_available_ram=minimum_available, result=result, last_event=records[-1] if records else None,
                       model=str(work/project/'bbox_1'/'model_1'),
                       osm_downloads=sum('OSM 数据下载完成' in r.get('message','') for r in records),
                       osm_cache_hits=sum('缓存的 OSM 查询结果' in r.get('message','') for r in records))
        summary['generation_completed'] = summary['exit_code']==0 and bool(result) and 'harness_error' not in summary
        summary['download_files'] = {}
        for filename in ('elevation.tif', 'building.geoparquet', 'street.json', 'river.json'):
            path = work/project/'bbox_1'/'download'/filename
            if path.is_file():
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                info = path.stat()
                summary['download_files'][filename] = dict(bytes=info.st_size, sha256=digest,
                                                          created_at=info.st_ctime, modified_at=info.st_mtime)
        summary['cold_verified'] = (not inputs_reused and not initial_entries and summary['osm_cache_hits']==0
                                    and summary['osm_downloads']>0 and len(summary['download_files'])==4
                                    and all(item['bytes']>0 for item in summary['download_files'].values()))
        if arguments.require_cold and not summary['cold_verified']:
            summary['generation_completed'] = False
        (work/'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary['generation_completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
