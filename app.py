"""Authenticated local job service for the desktop application."""
from __future__ import annotations
import hashlib
import json
import math
import mimetypes
import os
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
import yaml
from bootstrap import ASSETS, worker_command, whitebox_archive
from persistence import atomic_write_json
from process_control import start_worker, terminate_tree
import cache_management

ACTIVE = {'running', 'pausing', 'paused', 'resuming', 'cancelling'}
MAX_AREA_KM2 = 1000
SAMPLE_BBOX = [118.745, 32.010, 118.835, 32.090]
KINDS = {'inp', 'map', 'edges', 'nodes', 'subcatchments', 'geojson', 'bundle', 'log'}
DOWNLOADS = dict(elevation='elevation.tif', building='building.geoparquet', street='street.json', river='river.json')


def validate_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError('请输入项目名称')
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError('项目名称不能包含换行或控制字符')
    name = name.strip()
    if len(name) > 50:
        raise ValueError('项目名称应为 1–50 个字符，不能包含换行或控制字符')
    return name


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('请求必须为对象')
    name = validate_name(payload.get('name', ''))
    raw = payload.get('bbox')
    if not isinstance(raw, list) or len(raw) != 4 or any(isinstance(v, bool) or v is None or str(v).strip() == '' for v in raw):
        raise ValueError('请输入四个有效的经纬度数值')
    try:
        bbox = [float(v) for v in raw]
    except (ValueError, TypeError) as exc:
        raise ValueError('经纬度必须为数字') from exc
    if not all(math.isfinite(v) for v in bbox):
        raise ValueError('经纬度必须为有限数值')
    w, s, e, n = bbox
    if not (-180 <= w < e <= 180 and -85.051129 <= s < n <= 85.051129):
        raise ValueError('边界必须西 < 东、南 < 北；经度 ±180°，地图纬度 ±85.051129°')
    area = (e-w)*111.32*math.cos(math.radians((n+s)/2))*(n-s)*111.32
    if area > MAX_AREA_KM2:
        raise ValueError(f'单次生成范围不能超过约 {MAX_AREA_KM2} 平方公里')
    method = payload.get('method', 'withtopo')
    if method not in {'withtopo', 'separate'}:
        raise ValueError('排口推导方法无效')
    return name, bbox, method


class JobStore:
    def __init__(self, data: Path):
        self.data = Path(data).resolve()
        self.runs = self.data / 'runs'
        self.runs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs, self.processes, self.threads = {}, {}, {}
        self.pending_saves = {}
        self.token = secrets.token_urlsafe(32)
        folders = {p.parent for pattern in ('*/app_job.json', '*/app_job.recovery.json')
                   for p in self.runs.glob(pattern)}
        for folder in sorted(folders):
            snapshots = []
            for filename in ('app_job.json', 'app_job.recovery.json'):
                path = folder / filename
                try:
                    job = json.loads(path.read_text(encoding='utf-8'))
                    if not isinstance(job, dict) or not isinstance(job.get('status'), str):
                        continue
                    revision = job.get('_state_revision', 0)
                    if type(revision) is not int or revision < 0:
                        continue
                    job['id'] = folder.name
                    self.job_directory(job['id'])
                    snapshots.append((revision, path.stat().st_mtime_ns, filename, job))
                except (ValueError, OSError):
                    continue
            if not snapshots:
                continue
            _, _, filename, job = max(snapshots, key=lambda item: item[:3])
            job.pop('persistence_warning', None)
            interrupted = job['status'] in ACTIVE
            if interrupted:
                job.update(status='interrupted', stage='上次运行中断，可以重新生成', ended=time.time())
            self.jobs[job['id']] = job
            if interrupted or filename == 'app_job.recovery.json':
                self._save_best_effort(job)
        sample = self.data / 'sample'
        if (sample / 'network.geojson').exists():
            stats = json.loads((sample / 'stats.json').read_text(encoding='utf-8'))
            self.jobs['sample'] = dict(id='sample', name='南京主城区核心范围', bbox=SAMPLE_BBOX,
                status='complete', stage='南京历史成果', progress=100, created_at='2026-09-29',
                started=0, ended=0, stats=stats, model_dir='sample')
            try:
                saved = json.loads((sample/'app_job.json').read_text(encoding='utf-8'))
                self.jobs['sample']['name'] = validate_name(saved['name'])
            except (OSError, ValueError, KeyError):
                pass

    def job_directory(self, job_id):
        if job_id == 'sample':
            directory = self.data / 'sample'
            parent = self.data
        else:
            if not job_id or Path(job_id).name != job_id or '/' in job_id or '\\' in job_id:
                raise ValueError('任务路径无效')
            directory = self.runs / job_id
            parent = self.runs
        if directory.resolve().parent != parent.resolve() or directory.is_symlink() or directory.is_junction():
            raise ValueError('任务路径无效')
        return directory

    def model_dir(self, job):
        result = (self.data / job.get('model_dir', f"runs/{job['id']}/bbox_1/model_1")).resolve()
        if not result.is_relative_to(self.data):
            raise ValueError('成果路径无效')
        return result

    def file(self, job, kind):
        directory = self.model_dir(job)
        names = dict(map='network.png', edges='edges.geoparquet', nodes='nodes.geoparquet',
                     subcatchments='subcatchments.geoparquet', geojson='network.geojson', bundle='SWMM-model.zip')
        if kind == 'inp':
            return next(directory.glob('model_*.inp'), directory / 'missing.inp')
        if kind == 'log':
            return self.runs / job['id'] / 'run.log'
        return directory / names[kind]

    def public(self, job):
        result = {k: v for k, v in job.items() if k != 'model_dir' and not k.startswith('_')}
        result['files'] = [k for k in KINDS if self.file(job, k).is_file()]
        now = job.get('ended') or time.time()
        paused = job.get('paused_seconds', 0)
        if job.get('paused_since'):
            paused += max(0, now - job['paused_since'])
        result['elapsed'] = round(max(0, now - job.get('started', now) - paused)) if job.get('started') else 0
        return result

    def save(self, job):
        path = self.job_directory(job['id']) / 'app_job.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        # Revisions disambiguate old recovery files even on low-resolution FAT
        # timestamps or after a clock adjustment. Failed writes keep the same
        # revision in their independent recovery snapshot.
        job['_state_revision'] = job.get('_state_revision', 0) + 1
        atomic_write_json(path, self._snapshot(job))
        job.pop('persistence_warning', None)
        self.pending_saves.pop(job['id'], None)
        try:
            (path.parent / 'app_job.recovery.json').unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _snapshot(job):
        return {key: value for key, value in job.items() if key != 'persistence_warning'}

    def _save_best_effort(self, job):
        """Persistence is diagnostic state, never a reason to kill a worker."""
        try:
            self.save(job)
        except OSError as exc:
            first_failure = job['id'] not in self.pending_saves
            self.pending_saves[job['id']] = (job, time.monotonic() + 2)
            folder = self.job_directory(job['id'])
            job['persistence_warning'] = '任务状态暂时无法保存，软件会自动重试。请暂勿关闭软件。'
            try:
                atomic_write_json(folder / 'app_job.recovery.json', self._snapshot(job))
                job['persistence_warning'] = '任务状态已保存到恢复文件，正在重试更新历史记录。'
            except OSError:
                pass
            if first_failure:
                try:
                    with (folder / 'state-write.log').open('a', encoding='utf-8') as log:
                        log.write(f'{datetime.now().isoformat(timespec="seconds")} {exc!r}\n')
                except OSError:
                    pass

    def flush_pending(self, force=False):
        with self.lock:
            for job_id, (job, retry_at) in list(self.pending_saves.items()):
                if job_id not in self.jobs:
                    self.pending_saves.pop(job_id, None)
                elif force or time.monotonic() >= retry_at:
                    self._save_best_effort(job)

    def update(self, job, *, strict=False, **fields):
        with self.lock:
            previous = job.copy() if strict else None
            if 'progress' in fields:
                fields['progress'] = max(job.get('progress', 0), fields['progress'])
            job.update(fields)
            if strict:
                try:
                    self.save(job)
                except OSError:
                    job.clear()
                    job.update(previous)
                    raise
            else:
                self._save_best_effort(job)

    def create(self, payload):
        name, bbox, method = validate(payload)
        with self.lock:
            if any(j['status'] in ACTIVE for j in self.jobs.values()):
                raise RuntimeError('已有生成或暂停的任务，请先完成或取消')
            job_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
            job = dict(id=job_id, name=name, bbox=bbox, method=method, status='running', progress=0,
                stage='正在准备配置', created_at=datetime.now().isoformat(timespec='seconds'),
                started=time.time(), stats={}, model_dir=f'runs/{job_id}/bbox_1/model_1')
            folder = self.runs / job_id
            folder.mkdir()
            config = dict(base_dir=str(self.runs), project=job_id, bbox=bbox, run_model=False,
                          parameter_overrides={'outfall_derivation': {'method': method}})
            cached = self.data / 'cache' / hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
            mapping = DOWNLOADS
            sources = self.download_sources(bbox)
            sources.update({k: str(cached/v) for k,v in mapping.items()
                            if cache_management.is_usable_download(cached/v, k)})
            if sources:
                config['address_overrides'] = sources
            wbt = whitebox_archive()
            if wbt.exists():
                config.setdefault('address_overrides', {})['whiteboxtools_binaries_zip'] = str(wbt)
            config_path = folder / 'config.yml'
            config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
            self.save(job)
            self.jobs[job_id] = job
            thread = threading.Thread(target=self.run, args=(job, config_path, cached, mapping), daemon=True)
            self.threads[job_id] = thread
            thread.start()
            return self.public(job)

    def download_sources(self, bbox):
        """Reuse only stages known to have finished before a later stage began.

        A cancelled/failed download may leave an incomplete file. Never reuse
        that stage just because its file exists. Paths are rebuilt relative to
        the current data root, including after moving the portable folder.
        """
        sources = {}
        stage_rank = {'prepare': 0, 'elevation': 0, 'building': 1,
                      'street': 2, 'river': 3, 'graph': 4, 'write': 4,
                      'render': 4, 'verify': 4}
        for previous in self.jobs.values():
            if previous.get('bbox') != bbox or previous.get('id') == 'sample':
                continue
            count = 4 if previous.get('status') == 'complete' else stage_rank.get(previous.get('phase'), 0)
            directory = self.runs / previous['id'] / 'bbox_1' / 'download'
            for key, filename in list(DOWNLOADS.items())[:count]:
                path = directory / filename
                if cache_management.is_usable_download(path, key):
                    sources[key] = str(path)
        return sources

    def consume(self, job, event):
        with self.lock:
            if event.get('job_id') != job['id'] or job['status'] not in ACTIVE:
                return
            # Keep control acknowledgements while termination is in flight.
            # If Windows refuses termination, the worker will not resend an
            # already acknowledged pause/resume, so we must retain its state.
            if job['status'] == 'cancelling' and event.get('type') != 'control':
                return
            if event.get('type') == 'control':
                revision = event.get('revision')
                if (type(revision) is not int or revision < job.get('_last_control_ack', -1)
                        or revision > job.get('_control_revision', -1)
                        or event.get('state') not in {'paused', 'running'}):
                    return
                job['_last_control_ack'] = revision
                job['_last_control_state'] = event['state']
                total = event.get('paused_seconds', 0)
                if isinstance(total, (int, float)) and math.isfinite(total) and total >= 0:
                    job['paused_seconds'] = max(job.get('paused_seconds', 0), total)
                if event['state'] == 'running':
                    # The worker really left its wait even if a newer pause
                    # request is already pending. Preserve that newer status.
                    job.pop('paused_since', None)
                else:
                    at = event.get('paused_at', time.time())
                    job['paused_since'] = at if isinstance(at, (int, float)) and math.isfinite(at) else time.time()
                if revision != job.get('_control_revision') or job['status'] == 'cancelling':
                    self._save_best_effort(job)
                    return
                if event.get('state') == 'paused' and job['status'] in {'pausing', 'paused'}:
                    self.update(job, status='paused', stage='已暂停；点击“恢复生成”继续当前任务')
                elif event.get('state') == 'running' and job['status'] == 'resuming':
                    job.pop('paused_since', None)
                    self.update(job, status='running', stage=job.pop('_resume_stage', '正在继续生成'))
                return
            if event.get('type') == 'progress':
                fields = dict(progress=min(99, max(0, int(event['percent']))), phase=str(event['phase']))
                fields['stage' if job['status'] == 'running' else '_resume_stage'] = str(event['message'])
                self.update(job, **fields)
            elif event.get('type') == 'result':
                self.update(job, stats=event['stats'])
            elif event.get('type') == 'error':
                self.update(job, error=str(event['message']))

    def run(self, job, config_path, cached, mapping):
        proc = None
        events = config_path.parent / 'events.jsonl'
        try:
            events.touch()
            with (config_path.parent / 'run.log').open('w', encoding='utf-8') as log:
                with self.lock:
                    if job['status'] == 'cancelling':
                        self.update(job, status='cancelled', stage='已取消', ended=time.time())
                        return
                    env = os.environ.copy()
                    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', NUMBA_CACHE_DIR=str(self.data/'numba-cache'))
                    proc = start_worker(worker_command(config_path, events, job['id'], job['name']),
                        cwd=self.data, stdout=log, stderr=subprocess.STDOUT, env=env)
                    self.processes[job['id']] = proc
                with events.open(encoding='utf-8') as stream:
                    pending = ''
                    while True:
                        pending += stream.read()
                        while '\n' in pending:
                            line, pending = pending.split('\n', 1)
                            if line:
                                self.consume(job, json.loads(line))
                        self.flush_pending()
                        if proc.poll() is not None:
                            pending += stream.read()
                            for line in pending.splitlines():
                                if line:
                                    self.consume(job, json.loads(line))
                            break
                        time.sleep(.2)
            with self.lock:
                self._end_pause(job)
                if job['status'] == 'cancelling':
                    self.update(job, status='cancelled', stage='已取消，成果未标记完成', ended=time.time())
                elif proc.returncode == 0 and job.get('stats') and all(self.file(job,k).is_file() for k in ('inp','map','geojson','bundle')):
                    # A cache copy is optional. A completed model must not fail
                    # because a cache file is briefly locked or the disk is full.
                    try:
                        cache_management.preserve_downloads(self.data, config_path.parent,
                                                            job['bbox'], list(mapping))
                    except (OSError, RuntimeError, ValueError) as exc:
                        job['cache_warning'] = f'成果已完成，部分下载缓存未保存：{exc}'
                    self.update(job, status='complete', progress=100, stage='生成完成', ended=time.time())
                else:
                    self.update(job, status='failed', stage='生成失败，请查看详细日志',
                        error=job.get('error', f'计算进程退出，代码 {proc.returncode}'), ended=time.time())
        except Exception as exc:
            terminate_tree(proc)
            with self.lock:
                self._end_pause(job)
            self.update(job, status='cancelled' if job['status'] == 'cancelling' else 'failed',
                        stage='任务已停止', error=str(exc), ended=time.time())
        finally:
            with self.lock:
                self.processes.pop(job['id'], None)

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if job['status'] not in ACTIVE - {'cancelling'}:
                return self.public(job)
            previous = (job['status'], job.get('stage', ''))
            self.update(job, status='cancelling', stage='正在停止计算和网络下载，请稍候…')
            proc = self.processes.get(job_id)
            # Return immediately even when a network worker needs forceful termination.
            if proc:
                threading.Thread(target=self._terminate_job, args=(job, proc, previous), daemon=True).start()
            return self.public(job)

    @staticmethod
    def _end_pause(job):
        since = job.pop('paused_since', None)
        if since:
            job['paused_seconds'] = job.get('paused_seconds', 0) + max(0, time.time() - since)

    def _control(self, job_id, action):
        with self.lock:
            job = self.jobs[job_id]
            if job['status'] not in ACTIVE - {'cancelling'}:
                raise RuntimeError('任务已经结束或正在取消，不能暂停或恢复')
            if action == 'pause' and job['status'] in {'pausing', 'paused'}:
                return self.public(job)
            if action == 'resume' and job['status'] in {'running', 'resuming'}:
                return self.public(job)
            proc = self.processes.get(job_id)
            if proc and proc.poll() is not None:
                raise RuntimeError('计算已结束，正在整理任务结果')
            revision = job.get('_control_revision', 0) + 1
            atomic_write_json(self.job_directory(job_id) / 'task-control.json',
                              dict(action=action, revision=revision))
            job['_control_revision'] = revision
            job.setdefault('_resume_stage', job.get('stage', '正在继续生成'))
            if action == 'pause':
                self.update(job, status='pausing', stage='暂停中，当前下载分块或计算步骤结束后暂停')
            else:
                self.update(job, status='resuming', stage='正在恢复生成，请稍候…')
            return self.public(job)

    def pause(self, job_id):
        return self._control(job_id, 'pause')

    def resume(self, job_id):
        return self._control(job_id, 'resume')

    def _terminate_job(self, job, proc, previous=('running', '')):
        try:
            terminate_tree(proc)
        except Exception as exc:
            with self.lock:
                if job['status'] == 'cancelling' and proc.poll() is None:
                    status = previous[0]
                    if job.get('_last_control_ack', -1) == job.get('_control_revision', -2):
                        confirmed = job.get('_last_control_state')
                        if confirmed in {'paused', 'running'}:
                            status = confirmed
                    self.update(job, status=status, stage='停止失败，请再次点击停止并取消', error=str(exc))

    def rename(self, job_id, payload):
        if not isinstance(payload, dict):
            raise ValueError('请求必须为对象')
        name = validate_name(payload.get('name'))
        with self.lock:
            job = self.jobs[job_id]
            self.update(job, strict=True, name=name)
            return self.public(job)

    def cache_busy(self):
        return any(j['status'] in ACTIVE for j in self.jobs.values()) or any(t.is_alive() for t in self.threads.values())

    def cache_info(self):
        with self.lock:
            result = cache_management.inventory(self.data, [self.job_directory(j) for j in self.jobs])
            result['busy'] = self.cache_busy()
            return result

    def clear_cache(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('请求必须为对象')
        generation, maps = payload.get('generation', False), payload.get('map', False)
        if type(generation) is not bool or type(maps) is not bool or not (generation or maps):
            raise ValueError('请选择要清除的缓存类型')
        with self.lock:
            if self.cache_busy():
                raise RuntimeError('有生成或暂停的任务，请先完成或停止并取消后再清除缓存')
            return cache_management.clear(self.data, [self.job_directory(j) for j in self.jobs],
                                          generation=generation, map=maps)

    def delete(self, job_id, clear_cache=False):
        if type(clear_cache) is not bool:
            raise ValueError('清除缓存选项必须为是或否')
        with self.lock:
            job = self.jobs[job_id]
            thread = self.threads.get(job_id)
            if job['status'] in ACTIVE or (thread and thread.is_alive()):
                raise RuntimeError('请先停止并取消此任务，等待停止完成后再删除')
            directory = self.job_directory(job_id)
            # Another worker may be reusing a completed download from this run.
            for other in self.jobs.values():
                if other['status'] in ACTIVE:
                    config_path = self.job_directory(other['id']) / 'config.yml'
                    config = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
                    for source in config.get('address_overrides', {}).values():
                        if isinstance(source, str) and Path(source).resolve().is_relative_to(directory.resolve()):
                            raise RuntimeError('当前生成正在复用此项目的数据，请等待或停止生成后再删除')
            if clear_cache:
                self.clear_cache({'generation': True})
            elif job_id != 'sample':
                ranks = {'building': 1, 'street': 2, 'river': 3, 'graph': 4,
                         'write': 4, 'render': 4, 'verify': 4}
                count = 4 if job['status'] == 'complete' else ranks.get(job.get('phase'), 0)
                cache_management.preserve_downloads(self.data, directory, job['bbox'], list(DOWNLOADS)[:count])
            if directory.exists():
                shutil.rmtree(directory)
            del self.jobs[job_id]
            self.threads.pop(job_id, None)
            self.pending_saves.pop(job_id, None)
            return {'deleted': job_id}

    def close(self):
        for job in list(self.jobs.values()):
            if job['status'] in ACTIVE:
                self.cancel(job['id'])
        for thread in list(self.threads.values()):
            thread.join(timeout=15)
        self.flush_pending(force=True)


def make_server(store, port=0):
    from map_service import MapService
    maps = MapService(store.data)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, data, status=200, mime='application/json; charset=utf-8', cache='no-store'):
            body = json.dumps(data, ensure_ascii=False).encode() if not isinstance(data, bytes) else data
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', cache)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authorized(self):
            return secrets.compare_digest(self.headers.get('X-Session-Token', ''), store.token)

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path.startswith('/api/tiles/'):
                query = parse_qs(urlparse(self.path).query)
                if not secrets.compare_digest(query.get('token',[''])[0], store.token):
                    return self.send({'error':'会话校验失败'},403)
                try:
                    parts = path.strip('/').split('/')
                    if len(parts)!=5 or not parts[4].endswith('.png'):
                        raise ValueError('底图路径无效')
                    z,x,y = int(parts[2]),int(parts[3]),int(parts[4][:-4])
                    data, ttl = maps.tile(z,x,y,referer=f'http://127.0.0.1:{self.server.server_port}/')
                    return self.send(data,mime='image/png',cache=f'private, max-age={ttl}')
                except ValueError as exc:
                    return self.send({'error':str(exc)},400)
                except RuntimeError as exc:
                    return self.send({'error':str(exc)},502)
            if path.startswith('/api/'):
                if not self.authorized():
                    return self.send({'error': '会话校验失败，请重新打开软件'}, 403)
                if path == '/api/jobs':
                    with store.lock:
                        store.flush_pending()
                        return self.send([store.public(j) for j in sorted(store.jobs.values(),key=lambda j:j['created_at'],reverse=True)])
                if path == '/api/network':
                    return self.send(maps.status())
                if path == '/api/cache':
                    try:
                        return self.send(store.cache_info())
                    except (ValueError, RuntimeError, OSError) as exc:
                        return self.send({'error': str(exc)}, 409)
                parts = path.strip('/').split('/')
                if len(parts) == 3 and parts[1] == 'jobs':
                    with store.lock:
                        store.flush_pending()
                        job = store.jobs.get(parts[2])
                        return self.send(store.public(job) if job else {'error':'任务不存在'}, 200 if job else 404)
                if len(parts) == 4 and parts[1] == 'files' and parts[3] in KINDS:
                    with store.lock:
                        job = store.jobs.get(parts[2])
                        file = store.file(job, parts[3]) if job else None
                    if file and file.is_file():
                        mime = 'application/geo+json' if parts[3] == 'geojson' else 'application/octet-stream'
                        return self.send(file.read_bytes(), mime=mime)
                return self.send({'error':'资源不存在'},404)
            file = (ASSETS / 'web' / ('index.html' if path == '/' else path.lstrip('/'))).resolve()
            if not file.is_relative_to((ASSETS/'web').resolve()) or not file.is_file():
                return self.send({'error':'页面不存在'},404)
            return self.send(file.read_bytes(), mime=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

        def do_POST(self):
            if not self.authorized():
                return self.send({'error':'会话校验失败'},403)
            origin = self.headers.get('Origin')
            if origin and origin != f'http://127.0.0.1:{self.server.server_port}':
                return self.send({'error':'来源无效'},403)
            try:
                length = int(self.headers.get('Content-Length',0))
                if not 0 <= length <= 10000:
                    raise ValueError('请求大小无效')
                path = urlparse(self.path).path
                if path == '/api/network/check':
                    return self.send(maps.diagnose())
                if path == '/api/jobs':
                    return self.send(store.create(json.loads(self.rfile.read(length))),201)
                if path == '/api/cache/clear':
                    payload = json.loads(self.rfile.read(length))
                    if isinstance(payload, dict) and payload.get('map') is True:
                        with maps.cache_guard():
                            return self.send(store.clear_cache(payload))
                    return self.send(store.clear_cache(payload))
                parts = path.strip('/').split('/')
                if len(parts)==4 and parts[1]=='jobs' and parts[3]=='cancel':
                    return self.send(store.cancel(parts[2]))
                if len(parts)==4 and parts[1]=='jobs' and parts[3]=='pause':
                    return self.send(store.pause(parts[2]))
                if len(parts)==4 and parts[1]=='jobs' and parts[3]=='resume':
                    return self.send(store.resume(parts[2]))
                if len(parts)==4 and parts[1]=='jobs' and parts[3]=='rename':
                    return self.send(store.rename(parts[2], json.loads(self.rfile.read(length))))
                if len(parts)==4 and parts[1]=='jobs' and parts[3]=='delete':
                    payload = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(payload, dict):
                        raise ValueError('请求必须为对象')
                    return self.send(store.delete(parts[2], clear_cache=payload.get('clear_cache', False)))
                return self.send({'error':'接口不存在'},404)
            except KeyError:
                return self.send({'error':'任务不存在'},404)
            except RuntimeError as exc:
                return self.send({'error':str(exc)},409)
            except (ValueError, TypeError) as exc:
                return self.send({'error':str(exc)},400)
            except OSError as exc:
                return self.send({'error':f'文件读写失败：{exc}'},500)

    return ThreadingHTTPServer(('127.0.0.1',port), Handler)
