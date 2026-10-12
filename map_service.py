"""Local, authenticated map transport with HTTP-aware persistent tile caching."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

import requests

USER_AGENT = 'CNG/0.1 (desktop; based on SWMManywhere; https://github.com/ImperialCollegeLondon/SWMManywhere)'
TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
PNG = b'\x89PNG\r\n\x1a\n'


def error_message(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return 'TLS/SSL 连接失败，请检查网络或系统证书设置'
    if isinstance(exc, requests.exceptions.ProxyError):
        return '系统或环境中的代理连接失败，请检查代理是否正在运行'
    if isinstance(exc, requests.exceptions.Timeout):
        return '连接或响应超时，可稍后重试或切换网络'
    if isinstance(exc, requests.HTTPError):
        return f'服务返回 HTTP {exc.response.status_code}'
    if isinstance(exc, requests.ConnectionError):
        return '连接失败，请检查网络、DNS 或软件联网权限'
    return str(exc)[:180]


def cache_metadata(headers, now):
    control = headers.get('Cache-Control', '').lower()
    match = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)', control)
    expires = now + 7*86400
    try:
        if match:
            expires = now + max(0, int(match[1])-int(headers.get('Age', '0')))
        elif headers.get('Expires'):
            expires = parsedate_to_datetime(headers['Expires']).timestamp()
    except (ValueError, TypeError, OverflowError):
        pass
    if 'no-cache' in control:
        expires = now
    return dict(expires=expires, etag=headers.get('ETag'), modified=headers.get('Last-Modified'),
                store='no-store' not in control)


class MapService:
    def __init__(self, data):
        self.root = Path(data)/'map-cache'
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = Path(data)/'network.log'
        self.lock = threading.Lock()
        self.tiles_lock = [threading.Lock() for _ in range(64)]
        self.slots = threading.BoundedSemaphore(6)
        self.state = dict(downloaded=0, cached=0, failed=0, last_error='', last_success=None)
        self.check_lock = threading.Lock()
        self.check_result = None

    @contextmanager
    def cache_guard(self, timeout=30):
        """Wait for in-flight tile writes before inventory or explicit cleanup.

        Tiles acquire only one stripe lock. Taking all stripes in their fixed
        order cannot form a lock cycle; a shared deadline bounds a busy server.
        """
        acquired = []
        deadline = time.monotonic() + timeout
        try:
            for lock in self.tiles_lock:
                if not lock.acquire(timeout=max(0, deadline-time.monotonic())):
                    raise RuntimeError('底图仍在加载，暂时无法清理地图缓存，请稍后重试')
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def status(self):
        with self.lock:
            return dict(self.state)

    def record(self, kind, message=''):
        with self.lock:
            self.state[kind] += 1
            if kind == 'failed':
                self.state['last_error'] = message
            else:
                self.state['last_success'] = time.time()
            if kind != 'cached':
                with self.log.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(dict(time=time.time(),service='OSM 底图',status=kind,message=message),ensure_ascii=False)+'\n')

    @staticmethod
    def validate(z, x, y):
        if not 0 <= z <= 19 or not (0 <= x < 2**z and 0 <= y < 2**z):
            raise ValueError('底图坐标无效')

    def tile(self, z, x, y, referer=None):
        self.validate(z, x, y)
        key = f'{z}/{x}/{y}'
        with self.tiles_lock[hash(key)%len(self.tiles_lock)]:
            path = self.root/str(z)/str(x)/f'{y}.png'
            meta_path = path.with_suffix('.json')
            data, meta = None, {}
            try:
                data = path.read_bytes()
                if not data.startswith(PNG):
                    data = None
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass
            now = time.time()
            if data and meta.get('expires', 0) > now:
                self.record('cached')
                return data, max(0, int(meta['expires']-now))
            headers = {'User-Agent': USER_AGENT}
            if referer:
                headers['Referer'] = referer
            if data and meta.get('etag'):
                headers['If-None-Match'] = meta['etag']
            if data and meta.get('modified'):
                headers['If-Modified-Since'] = meta['modified']
            acquired = self.slots.acquire(timeout=20)
            try:
                if not acquired:
                    raise requests.Timeout('本地下载队列繁忙')
                response = requests.get(TILE_URL.format(z=z,x=x,y=y), headers=headers, timeout=(8,20))
                response.raise_for_status()
                if response.status_code != 304:
                    data = response.content
                if not data or not data.startswith(PNG):
                    raise ValueError('底图服务未返回有效 PNG 图片')
                updated = cache_metadata(response.headers, time.time())
                if response.status_code == 304:
                    updated['etag'] = updated['etag'] or meta.get('etag')
                    updated['modified'] = updated['modified'] or meta.get('modified')
                if updated['store']:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_suffix('.tmp')
                    temp.write_bytes(data); temp.replace(path)
                    temp = meta_path.with_suffix('.tmp')
                    temp.write_text(json.dumps(updated), encoding='utf-8'); temp.replace(meta_path)
                else:
                    path.unlink(missing_ok=True); meta_path.unlink(missing_ok=True)
                self.record('downloaded')
                return data, max(0, int(updated['expires']-time.time()))
            except (requests.RequestException, OSError, ValueError) as exc:
                reason = error_message(exc)
                self.record('failed', reason)
                raise RuntimeError(reason) from exc
            finally:
                if acquired:
                    self.slots.release()

    def diagnose(self):
        # Explicit user action, memoized briefly to avoid repeatedly probing public services.
        with self.check_lock:
            if self.check_result and time.time()-self.check_result['checked_at'] < 30:
                return self.check_result
            services = [
                ('地图底图', 'https://tile.openstreetmap.org/6/53/25.png', 'HEAD'),
                ('高程目录', 'https://planetarycomputer.microsoft.com/api/stac/v1/', 'GET'),
                ('建筑目录', 'https://stac.overturemaps.org/catalog.json', 'GET'),
                ('OSM 主服务', 'https://overpass-api.de/api/status', 'GET'),
                ('OSM 备用服务 VK Maps', 'https://maps.mail.ru/osm/tools/overpass/api/status', 'GET'),
                ('OSM 备用服务 Private.coffee', 'https://overpass.private.coffee/api/status', 'GET')]
            def check(service):
                name, url, method = service
                start = time.monotonic()
                try:
                    response = requests.request(method, url, headers={'User-Agent': USER_AGENT}, timeout=(5,10))
                    response.raise_for_status()
                    ok, message = True, f'HTTP {response.status_code}，接口可达'
                except requests.RequestException as exc:
                    ok, message = False, error_message(exc)
                return dict(name=name,host=urlsplit(url).hostname,ok=ok,message=message,seconds=round(time.monotonic()-start,1))
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(check, services))
            self.check_result = dict(checked_at=time.time(),services=results,tiles=self.status())
            return self.check_result
