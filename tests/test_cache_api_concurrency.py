"""Real local HTTP/cache races; no external requests or user data."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

import map_service
from app import JobStore, make_server
from test_history import finished_job
from test_map_service import response


@pytest.fixture
def local_api(tmp_path):
    store = JobStore(tmp_path / 'data')
    server = make_server(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, payload=None):
        headers = {'X-Session-Token': store.token, 'Origin': base}
        raw = None if payload is None else json.dumps(payload).encode()
        with urllib.request.urlopen(urllib.request.Request(base + path, data=raw, headers=headers), timeout=8) as result:
            return result.read()
    try:
        yield store, request
    finally:
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_map_cleanup_waits_for_inflight_tile_then_next_request_downloads_again(local_api, monkeypatch):
    store, request = local_api
    entered, release, cleanup_started = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def download(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5)
        return response(**{'Cache-Control': 'max-age=3600'})
    monkeypatch.setattr(map_service.requests, 'get', download)
    tile_url = '/api/tiles/3/4/2.png?token=' + store.token
    def clear():
        cleanup_started.set()
        return json.loads(request('/api/cache/clear', {'map': True}))
    with ThreadPoolExecutor(max_workers=2) as executor:
        tile = executor.submit(request, tile_url)
        try:
            assert entered.wait(timeout=3)
            cleanup = executor.submit(clear)
            assert cleanup_started.wait(timeout=3)
            # Clearing cannot finish while this tile still owns its write lock.
            assert not cleanup.done()
        finally:
            release.set()
        original = tile.result(timeout=5)
        result = cleanup.result(timeout=5)
    assert result['cleared_files'] == 2
    assert store.cache_info()['map']['files'] == 0
    assert request(tile_url) == original
    assert len(calls) == 2
    assert store.cache_info()['map']['files'] == 2


@pytest.mark.skipif(os.name != 'nt', reason='Requires real Windows file sharing')
def test_locked_cache_reports_failure_and_retry_preserves_model_and_history(local_api):
    # This is a native file handle, not mocked unlink behavior.
    script = Path(__file__).resolve().parents[1] / 'packaging/validate_state_lock.py'
    spec = importlib.util.spec_from_file_location('native_cache_lock_qa', script)
    native_lock_qa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native_lock_qa)
    store, request = local_api
    job = finished_job(store)
    model = store.file(job, 'inp')
    before = model.read_bytes()
    view = store.data / 'map-view.json'
    view.write_bytes(b'{"zoom":12}')
    cached = store.data / 'cache' / 'locked.json'
    cached.parent.mkdir()
    cached.write_bytes(b'cached payload')
    held = native_lock_qa.NoDeleteShare(cached)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            request('/api/cache/clear', {'generation': True})
        assert error.value.code == 409
        detail = json.loads(error.value.read())['error']
        assert '未全部完成' in detail and '已删除 0 个文件' in detail
        assert model.read_bytes() == before and cached.exists()
        assert JobStore(store.data).jobs[job['id']]['status'] == 'complete'
    finally:
        held.close()
    result = json.loads(request('/api/cache/clear', {'generation': True}))
    assert result['cleared_files'] == 1
    assert model.read_bytes() == before and view.read_bytes() == b'{"zoom":12}'
    assert JobStore(store.data).jobs[job['id']]['status'] == 'complete'
