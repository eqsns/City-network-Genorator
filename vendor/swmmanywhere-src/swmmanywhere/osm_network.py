"""Bounded Overpass transport for the desktop's pinned OSMnx version.

Keep OSMnx's queries and response cache; replace its unbounded recursive HTTP
retry/slot polling. Public endpoints: wiki.openstreetmap.org/wiki/Overpass_API.
This runs only in the isolated, single-job calculation process.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
from queue import Empty, Queue
import re
import ssl
from threading import Event, Thread
import time
from urllib.parse import urlsplit

import requests
from osmnx import _http, _overpass, settings
from swmmanywhere.logging import logger
from swmmanywhere import osm_cache
from swmmanywhere import progress as execution_progress

USER_AGENT = 'CNG/0.1 (desktop; https://github.com/ImperialCollegeLondon/SWMManywhere)'
MIRRORS = ('https://maps.mail.ru/osm/tools/overpass/api',
           'https://overpass.private.coffee/api')
_notify = ContextVar('osm_download_status', default=None)
ENDPOINT_SECONDS = 90
QUERY_SECONDS = 240
STAGE_SECONDS = 480
MAX_STAGE_SECONDS = 3600
WORKLOAD_QUERY_SECONDS = 60
HEARTBEAT_SECONDS = 10
SERVER_QUERY_SECONDS = 25
SERVER_MAXSIZE_BYTES = 64 * 1024 * 1024
SHORT_SLOT_WAIT_SECONDS = 30
TRANSIENT_HTTP_STATUS = (429, 502, 503, 504)


def _active_time():
    # Pass this module's clock explicitly: existing CLI behavior and tests
    # patching time.monotonic remain unchanged when no desktop hook is set.
    return execution_progress.active_monotonic(time.monotonic)


def bounded_query(query):
    """Reserve modest server resources without altering filters or cache keys.

    Overpass admission control considers the requested limits, not only actual
    usage. Desktop graph requests are subdivided so they do not need the native
    180 second / 512 MB reservation. Only rewrite the opening QL settings block;
    text inside tag filters and comments later in the query is data.
    """
    opening = re.match(r'\s*(?:\[[^\]\r\n]*\]\s*)+', query)
    if not opening:
        return query
    prefix = opening.group()
    for name, limit in (('timeout', SERVER_QUERY_SECONDS), ('maxsize', SERVER_MAXSIZE_BYTES)):
        pattern = rf'\[{name}\s*:\s*(\d+)\s*\]'
        if re.search(pattern, prefix):
            prefix = re.sub(pattern, lambda match: f'[{name}:{min(int(match[1]), limit)}]', prefix)
        else:
            prefix += f'[{name}:{limit}]'
    return prefix + query[opening.end():]


class EndpointUnavailable(requests.RequestException):
    def __init__(self, message, cooldown=60, slot=False):
        super().__init__(message)
        self.cooldown = cooldown
        self.slot = slot


def check_response(response):
    """Preserve server cooldowns for transient status and interpreter failures."""
    if response.status_code in TRANSIENT_HTTP_STATUS:
        raise EndpointUnavailable(f'服务暂不可用（HTTP {response.status_code}）',
                                  cooldown=retry_after(response.headers.get('Retry-After')))
    response.raise_for_status()


def recoverable_error(exc):
    # Requests wraps urllib3/OpenSSL errors. Only known interrupted TLS streams
    # are transient; certificate verification and unknown TLS failures are not.
    if isinstance(exc, requests.exceptions.SSLError):
        pending, seen, messages = [exc], set(), []
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            if isinstance(error, ssl.SSLCertVerificationError):
                return False
            messages.append(str(error).lower())
            pending.extend(value for value in (
                getattr(error, '__cause__', None), getattr(error, '__context__', None),
                getattr(error, 'reason', None), *getattr(error, 'args', ()))
                if isinstance(value, BaseException))
        text = ' '.join(messages)
        if 'certificate' in text or 'hostname mismatch' in text:
            return False
        return any(token in text for token in ('unexpected_eof', 'unexpected eof',
                                               'eof occurred', 'temporary eof', 'connection reset'))
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in TRANSIENT_HTTP_STATUS
    # iter_content wraps interrupted bodies and damaged compressed transfers
    # separately from ConnectionError. Discard the entire body and retry from
    # scratch under the same cooldown/attempt limits; never parse partial bytes.
    return isinstance(exc, (EndpointUnavailable, requests.Timeout, requests.ConnectionError,
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ContentDecodingError))


def retry_after(value):
    """Honor both forms of Retry-After without making other services wait."""
    try:
        return max(30, int(value))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            return max(30, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 60


@contextmanager
def download_status(callback):
    token = _notify.set(callback)
    try:
        yield
    finally:
        _notify.reset(token)


def notice(message):
    logger.info(message)
    if callback := _notify.get():
        callback(message)


def slot_wait(text):
    """Return the advertised slot wait; rate limit zero means unrestricted."""
    if re.search(r'^Rate limit:\s*0\s*$', text, re.M):
        return 0
    if re.search(r'^\s*[1-9]\d* slots? available', text, re.M):
        return 0
    dates = re.findall(r'Slot available after:\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)', text)
    if dates:
        earliest = min(datetime.fromisoformat(value.replace('Z', '+00:00')) for value in dates)
        return max(1, math.ceil((earliest - datetime.now(timezone.utc)).total_seconds()))
    # Unknown/busy status: conservatively defer this host, never poll recursively.
    return 60


class OverpassClient:
    def __init__(self):
        self.working = None
        self.cooldown = {}
        # Slot cooldowns belong to the service, not to an individual tile.
        # A mirror can finish several tiles before this service is ready again.
        self.queued = set()
        self.recoverable = set()
        self.stage_deadline = None
        self.stage_started = None
        self.stage_outer_deadline = None
        self.stage_configured = False
        self.query_number = 0

    def _fetch(self, base, data, updates, abandoned):
        """One attempt. Never mutate process-global DNS from a transport thread."""
        host = urlsplit(base).hostname
        with requests.Session() as session:
            # This is a native CNG data request, not a visit from the OSMnx
            # project's web page. Its default Referer misidentifies our app and
            # was rejected with HTTP 429 in same-query transport comparisons.
            session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json',
                                    'Accept-Language': settings.http_accept_language})
            # Reuse the same session/connection for status and query. In contrast
            # to OSMnx's separate requests, this also avoids global DNS patching
            # and a DNS-over-HTTPS dependency. DNS itself is covered by the outer
            # wall-clock deadline, not just requests' socket inactivity timeout.
            if settings.overpass_rate_limit and base not in MIRRORS:
                response = session.get(base.rstrip('/')+'/status', timeout=(10, 15), **settings.requests_kwargs)
                check_response(response)
                wait = slot_wait(response.text)
                if 0 < wait <= SHORT_SLOT_WAIT_SECONDS:
                    # A healthy server can advertise a brief per-user cooldown
                    # between chunks. Honor that exact slot time once rather
                    # than spend 90 seconds on a less reachable mirror. The
                    # outer wall deadline and cancellation still apply.
                    updates.put(('notice', f'{host} 短暂排队，按服务提示等待 {wait} 秒后继续（可随时取消）'))
                    if abandoned.wait(wait):
                        raise requests.Timeout('已停止等待此服务')
                elif wait:
                    known_slot = bool(re.search(r'Slot available after:\s*\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', response.text))
                    raise EndpointUnavailable(f'服务排队约 {wait} 秒', cooldown=wait, slot=known_slot)
            if abandoned.is_set():
                raise requests.Timeout('已停止等待此服务')
            updates.put(('notice', f'正在从 {host} 下载 OSM 数据（本服务最多等待 {ENDPOINT_SECONDS} 秒，可随时取消）'))
            # A smaller server execution timeout prevents abandoned queries
            # continuing to consume the public server's resources for 180s.
            # Cache keys retain the original logical query, so old complete
            # results remain reusable; changing a timeout does not change data.
            query = dict(data)
            if isinstance(query.get('data'), str):
                query['data'] = bounded_query(query['data'])
            endpoint = base.rstrip('/')+'/interpreter'
            query_url = requests.Request('GET', endpoint, params=query).prepare().url
            # Use GET for compact read-only queries and POST for custom QL that
            # would otherwise exceed a conservative URL length.
            if len(query_url.encode('utf-8')) <= 7000:
                request = session.get
                arguments = {'params': query}
            else:
                request = session.post
                arguments = {'data': query}
            with request(endpoint, **arguments,
                         timeout=(10, 70), stream=True, **settings.requests_kwargs) as response:
                check_response(response)
                chunks = []
                for chunk in response.iter_content(chunk_size=65536):
                    if abandoned.is_set():
                        raise requests.Timeout('已停止接收此服务的数据')
                    chunks.append(chunk)
                result = json.loads(b''.join(chunks))
                if not isinstance(result, dict) or not isinstance(result.get('elements'), list):
                    raise requests.RequestException('服务未返回有效 OSM 数据')
                if 'remark' in result:
                    remark = str(result['remark'])
                    message = '服务查询未完成：'+remark[:200]
                    # Overpass can return HTTP 200 plus partial elements when
                    # its execution budget expires. Retry only an explicit
                    # timeout, using the same query/resource limits.
                    if re.search(r'\bquery\s+timed\s+out\b|\bruntime\s+(?:error\s*:\s*)?(?:query\s+)?(?:timed\s+out|timeout)\b',
                                 remark, re.I):
                        raise EndpointUnavailable(message, cooldown=60)
                    raise requests.RequestException(message)
                return result

    def _attempt(self, base, data, deadline):
        """Bound DNS, response headers and slow-drip bodies by actual elapsed time.

        requests timeout alone is only an inactivity timeout. The daemon owns
        its session and cannot change shared settings/cache. After abandonment
        its result is ignored and its next body chunk closes the connection.
        All of this runs in the cancellable, isolated calculation process.
        """
        updates = Queue()
        abandoned = Event()
        started = _active_time()
        end = min(deadline, started + ENDPOINT_SECONDS)
        def fetch():
            try:
                updates.put(('done', self._fetch(base, data, updates, abandoned)))
            except Exception as exc:
                updates.put(('error', exc))
        Thread(target=fetch, daemon=True, name='overpass-download').start()
        host = urlsplit(base).hostname
        try:
            while True:
                remaining = end - _active_time()
                if remaining <= 0:
                    raise requests.Timeout(f'本服务等待已达到 {int(end-started)} 秒上限')
                try:
                    kind, value = updates.get(timeout=min(HEARTBEAT_SECONDS, remaining))
                except Empty:
                    elapsed = int(_active_time()-started)
                    notice(f'正在等待 {host} 返回 OSM 数据，已用 {elapsed} 秒（可随时取消）')
                    continue
                if _active_time() >= end:
                    raise requests.Timeout('本服务已达到等待上限')
                if kind == 'notice':
                    notice(value)
                elif kind == 'error':
                    raise value
                else:
                    return value
        finally:
            abandoned.set()

    def request(self, data):
        # Only pause between whole OSM queries; progress notices during an
        # active HTTP attempt must never stop its timeout supervision.
        execution_progress.checkpoint()
        self.query_number += 1
        deadline = min(_active_time()+QUERY_SECONDS, self.stage_deadline or float('inf'))
        endpoints = list(dict.fromkeys(filter(None, (self.working, settings.overpass_url, *MIRRORS))))
        # Check every endpoint cache before initiating any network request.
        keys = {base: str(requests.Request('GET', base.rstrip('/')+'/interpreter', params=data).prepare().url)
                for base in endpoints}
        for base, key in keys.items():
            cached = osm_cache.retrieve(key)
            if isinstance(cached, dict) and isinstance(cached.get('elements'), list) and 'remark' not in cached:
                notice('正在读取缓存的 OSM 查询结果')
                return cached
        errors = []
        attempts = dict.fromkeys(endpoints, 0)
        notice(f'正在获取第 {self.query_number} 个 OSM 查询（本查询最多 {QUERY_SECONDS} 秒）')

        def attempt(base):
            attempts[base] += 1
            host = urlsplit(base).hostname
            try:
                notice(f'正在连接 OSM 服务 {host}（连接超时 10 秒）')
                result = self._attempt(base, data, deadline)
                if not osm_cache.save(keys[base], result) and settings.use_cache:
                    notice('OSM 数据下载成功，但缓存未能保存；继续使用本次完整数据')
                self.working = base
                self.queued.discard(base)
                self.recoverable.discard(base)
                self.cooldown.pop(base, None)
                notice(f'OSM 数据下载完成：{host}，{len(result["elements"])} 个要素')
                return result
            except (requests.RequestException, ValueError, OSError) as exc:
                if isinstance(exc, requests.exceptions.SSLError):
                    reason = 'SSL 连接中断'
                elif isinstance(exc, requests.exceptions.Timeout):
                    reason = '连接或响应超时（已停止等待此服务）'
                elif isinstance(exc, requests.HTTPError):
                    reason = f'HTTP {exc.response.status_code}'
                else:
                    reason = str(exc)[:240]
                errors.append(f'{host}：{reason}')
                cooldown = getattr(exc, 'cooldown', 60)
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    cooldown = retry_after(exc.response.headers.get('Retry-After'))
                self.cooldown[base] = max(self.cooldown.get(base, 0), _active_time()+cooldown)
                if recoverable_error(exc):
                    self.recoverable.add(base)
                else:
                    self.recoverable.discard(base)
                if getattr(exc, 'slot', False):
                    self.queued.add(base)
                else:
                    # A new HTTP/network failure supersedes an older slot
                    # announcement. Never use it to bypass a later Retry-After.
                    self.queued.discard(base)
                notice(f'{host} {reason}；立即检查备用服务')
            return None

        for base in endpoints:
            if _active_time() >= deadline:
                errors.append('已达到 OSM 查询或下载阶段的等待上限')
                break
            if self.cooldown.get(base, 0) > _active_time():
                errors.append(f'{urlsplit(base).hostname}：暂时限流或不可用')
                if base in self.recoverable:
                    notice(f'{urlsplit(base).hostname} 仍在前一分块的冷却等待中，将在预算内自动恢复，无需取消重试')
                continue
            if (result := attempt(base)) is not None:
                return result

        # A slot or transient network failure can recover while mirrors run.
        # Retain cooldowns across tiles, honor Retry-After, and allow at most two
        # attempts per service and query, always under the original deadline.
        while _active_time() < deadline:
            candidates = [base for base in endpoints
                          if base in self.recoverable and attempts[base] < 2]
            if not candidates:
                break
            base = min(candidates, key=lambda value: self.cooldown.get(value, 0))
            ready = self.cooldown[base]
            if ready < deadline:
                host = urlsplit(base).hostname
                while (remaining := ready-_active_time()) > 0:
                    notice(f'正在等待 {host} 冷却结束，约剩 {math.ceil(remaining)} 秒；自动恢复，无需取消重试（可随时取消）')
                    time.sleep(min(HEARTBEAT_SECONDS, remaining, max(0, deadline-_active_time())))
                    if _active_time() >= deadline:
                        break
                if _active_time() < deadline:
                    notice(f'{host} 冷却等待已结束，正在自动恢复，无需取消重试（本查询第 {attempts[base]+1}/2 次尝试）')
                    if (result := attempt(base)) is not None:
                        return result
            else:
                errors.append('服务冷却或排队时间超过本次查询剩余预算')
                break
        raise requests.ConnectionError('OSM 数据服务均未连接成功。'+ '；'.join(errors)
                                       +'。未使用不完整数据。可缩小范围后重试，或稍后更换网络；详细原因已保存到日志。')


client = OverpassClient()


def configure_workload(total_queries):
    """Set this graph's budget once, measured from its original start time.

    A downloader may call this outside bounded_requests (e.g. direct library
    use); that has no effect. Nested graph contexts never extend their parent.
    """
    if client.stage_started is None or client.stage_configured:
        return
    total_queries = max(1, int(total_queries))
    budget = min(MAX_STAGE_SECONDS, max(STAGE_SECONDS, total_queries * WORKLOAD_QUERY_SECONDS))
    client.stage_deadline = min(client.stage_started + budget,
                                client.stage_outer_deadline or float('inf'))
    client.stage_configured = True
    notice(f'本图共 {total_queries} 个 OSM 查询，下载阶段最多 {budget // 60} 分钟；支持自动恢复和手动取消')


@contextmanager
def bounded_requests():
    from swmmanywhere.osm_queries import download_network
    execution_progress.checkpoint()
    original = _overpass._overpass_request
    original_download = _overpass._download_overpass_network
    previous = (client.stage_deadline, client.stage_started,
                client.stage_outer_deadline, client.stage_configured, client.query_number)
    client.stage_started = _active_time()
    client.stage_outer_deadline = client.stage_deadline
    client.stage_deadline = min(client.stage_started+STAGE_SECONDS,
                                client.stage_outer_deadline or float('inf'))
    client.stage_configured = False
    client.query_number = 0
    _overpass._overpass_request = client.request
    _overpass._download_overpass_network = download_network
    try:
        yield
    finally:
        _overpass._overpass_request = original
        _overpass._download_overpass_network = original_download
        (client.stage_deadline, client.stage_started, client.stage_outer_deadline,
         client.stage_configured, client.query_number) = previous
