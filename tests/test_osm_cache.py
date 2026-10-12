import json
from hashlib import sha1

import bootstrap
import pytest
import requests
from osmnx import _http, settings
from swmmanywhere import osm_cache


KEY = 'https://overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D%3Bnode%281%29%3Bout%3B'
COMPLETE = {'elements': [{'type': 'node', 'id': 1, 'lat': 32.0, 'lon': 118.0}]}


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'cache_folder', tmp_path / 'cache')
    monkeypatch.setattr(settings, 'use_cache', True)
    return tmp_path / 'cache'


def test_original_logical_url_keeps_legacy_osmnx_cache_path(cache):
    assert osm_cache.save(KEY, COMPLETE)
    expected = cache / (sha1(KEY.encode('utf-8')).hexdigest() + '.json')
    assert expected == _http._resolve_cache_filepath(KEY)
    assert json.loads(expected.read_text(encoding='utf-8')) == COMPLETE
    assert _http._retrieve_from_cache(KEY) == COMPLETE
    assert osm_cache.retrieve(KEY) == COMPLETE


@pytest.mark.parametrize('content', [b'{"elements": [', b'\xff', b'[]', b'{}',
                                     b'{"elements":{}}', b'{"elements":[],"remark":"partial"}',
                                     b'{"elements":[],"remark":""}'])
def test_invalid_response_is_a_miss_without_deleting_file(cache, content):
    target = _http._resolve_cache_filepath(KEY)
    target.parent.mkdir()
    target.write_bytes(content)
    assert osm_cache.retrieve(KEY) is None
    assert target.read_bytes() == content


def test_empty_complete_response_is_cached(cache):
    assert osm_cache.save(KEY, {'elements': []})
    assert osm_cache.retrieve(KEY) == {'elements': []}


def test_failed_atomic_replace_preserves_previous_complete_cache(cache, monkeypatch):
    assert osm_cache.save(KEY, COMPLETE)
    target = _http._resolve_cache_filepath(KEY)
    previous = target.read_bytes()
    def cannot_replace(source, destination):
        assert source.suffix == '.tmp'
        assert source.parent == target.parent
        assert target.read_bytes() == previous
        assert json.loads(source.read_text(encoding='utf-8')) == {'elements': []}
        raise PermissionError('locked cache')
    monkeypatch.setattr(osm_cache.os, 'replace', cannot_replace)
    assert osm_cache.save(KEY, {'elements': []}) is False
    assert target.read_bytes() == previous
    assert osm_cache.retrieve(KEY) == COMPLETE
    assert list(cache.glob('*.tmp')) == []


def test_incomplete_and_unserializable_data_cannot_damage_old_cache(cache):
    assert osm_cache.save(KEY, COMPLETE)
    previous = _http._resolve_cache_filepath(KEY).read_bytes()
    for invalid in ({'elements': [], 'remark': 'partial'}, {'elements': {}},
                    {'elements': [object()]}, {'elements': [float('nan')]}):
        assert osm_cache.save(KEY, invalid) is False
        assert _http._resolve_cache_filepath(KEY).read_bytes() == previous
    assert list(cache.glob('*.tmp')) == []


def test_disabled_cache_neither_reads_nor_writes(cache, monkeypatch):
    assert osm_cache.save(KEY, COMPLETE)
    monkeypatch.setattr(settings, 'use_cache', False)
    assert osm_cache.retrieve(KEY) is None
    assert osm_cache.save(KEY, {'elements': []}) is False
    assert json.loads(_http._resolve_cache_filepath(KEY).read_text(encoding='utf-8')) == COMPLETE
    assert osm_cache.save(KEY + '&another=1', COMPLETE) is False
    assert len(list(cache.iterdir())) == 1


def test_missing_cache_and_leftover_temp_files_are_not_read(cache):
    assert osm_cache.retrieve(KEY) is None
    cache.mkdir()
    target = _http._resolve_cache_filepath(KEY)
    target.with_suffix('.json.abandoned.tmp').write_text(json.dumps(COMPLETE), encoding='utf-8')
    assert osm_cache.retrieve(KEY) is None


def test_corrupt_cached_query_is_downloaded_again_and_repaired(cache, monkeypatch):
    from swmmanywhere import osm_network as net
    monkeypatch.setattr(net.settings, 'overpass_url', 'https://overpass-api.de/api')
    monkeypatch.setattr(net, 'MIRRORS', ())
    data = {'data': '[out:json];node(1);out;'}
    key = str(requests.Request('GET', settings.overpass_url + '/interpreter', params=data).prepare().url)
    target = _http._resolve_cache_filepath(key)
    cache.mkdir()
    target.write_text('{"elements": [', encoding='utf-8')
    client = net.OverpassClient()
    calls = []
    def downloaded(base, request, deadline):
        calls.append((base, request))
        return COMPLETE
    monkeypatch.setattr(client, '_attempt', downloaded)
    assert client.request(data) == COMPLETE
    assert len(calls) == 1
    assert osm_cache.retrieve(key) == COMPLETE
    assert client.request(data) == COMPLETE
    assert len(calls) == 1


def test_cache_write_failure_does_not_retry_successful_network_response(cache, monkeypatch):
    from swmmanywhere import osm_network as net
    client = net.OverpassClient()
    calls = []
    def downloaded(base, request, deadline):
        calls.append(base)
        return COMPLETE
    monkeypatch.setattr(client, '_attempt', downloaded)
    monkeypatch.setattr(osm_cache, 'save', lambda *args: False)
    assert client.request({'data': '[out:json];node(1);out;'}) == COMPLETE
    assert len(calls) == 1
    assert client.working == calls[0]
