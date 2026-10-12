"""Exercise cache reuse and failures entirely within pytest temporary data."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

import app
import cache_management as cache
from app import JobStore
from test_history import finished_job


def shared(store, bbox, filename):
    digest = hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
    target = store.data / 'cache' / digest / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def download(store, job, filename, content):
    target = store.job_directory(job['id']) / 'bbox_1' / 'download' / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def snapshot_model(store, job):
    return {p.relative_to(store.model_dir(job)).as_posix(): p.read_bytes()
            for p in store.model_dir(job).rglob('*') if p.is_file()}


def create_without_worker(store, monkeypatch, bbox):
    """Exercise the real create/config path without downloading public data."""
    def finished_without_worker(job, *args):
        job['status'] = 'cancelled'
    monkeypatch.setattr(store, 'run', finished_without_worker)
    created = store.create({'name': '缓存隔离验收', 'bbox': bbox})
    store.threads[created['id']].join(timeout=5)
    assert not store.threads[created['id']].is_alive()
    return yaml.safe_load((store.job_directory(created['id']) / 'config.yml').read_text(encoding='utf-8'))


def test_delete_keep_cache_can_reuse_after_restart(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = finished_job(store)
    contents = {name: ('complete ' + name).encode() for name in app.DOWNLOADS.values()}
    graph = b'{"directed":true,"multigraph":true,"graph":{},"nodes":[],"links":[]}'
    contents.update({'street.json': graph, 'river.json': graph})
    for filename, body in contents.items():
        download(store, job, filename, body)
    bbox = job['bbox']
    store.delete(job['id'])
    restarted = JobStore(tmp_path)
    assert job['id'] not in restarted.jobs
    config = create_without_worker(restarted, monkeypatch, bbox)
    for key, filename in app.DOWNLOADS.items():
        target = Path(config['address_overrides'][key])
        assert target.is_relative_to(tmp_path / 'cache')
        assert target.read_bytes() == contents[filename]


def test_clear_generation_cache_rebuild_has_no_stale_input_overrides(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = finished_job(store)
    before = snapshot_model(store, job)
    for filename in app.DOWNLOADS.values():
        download(store, job, filename, b'local complete')
        shared(store, job['bbox'], filename).write_bytes(b'shared complete')
    view = tmp_path / 'map-view.json'
    view.write_bytes(b'{"zoom":11}')
    assert store.clear_cache({'generation': True})['cleared_files'] == 8
    assert snapshot_model(store, job) == before
    assert view.read_bytes() == b'{"zoom":11}'
    config = create_without_worker(store, monkeypatch, job['bbox'])
    assert not set(app.DOWNLOADS).intersection(config.get('address_overrides', {}))
    assert snapshot_model(store, job) == before


@pytest.mark.parametrize('filename', list(app.DOWNLOADS.values()))
def test_empty_shared_cache_is_not_sent_to_new_worker(tmp_path, monkeypatch, filename):
    store = JobStore(tmp_path)
    bbox = [118.77, 32.02, 118.78, 32.03]
    shared(store, bbox, filename).write_bytes(b'')
    config = create_without_worker(store, monkeypatch, bbox)
    key = next(key for key, name in app.DOWNLOADS.items() if name == filename)
    assert key not in config.get('address_overrides', {}), 'empty cache would bypass download and fail later'


def test_empty_shared_cache_cannot_override_completed_project_input(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = finished_job(store)
    complete = download(store, job, 'street.json',
                        b'{"directed":true,"multigraph":true,"graph":{},"nodes":[],"links":[]}')
    shared(store, job['bbox'], 'street.json').write_bytes(b'')
    config = create_without_worker(store, monkeypatch, job['bbox'])
    assert Path(config['address_overrides']['street']) == complete


@pytest.mark.parametrize('failure', ['copy_partial', 'replace', 'flush'])
def test_cache_copy_failure_during_delete_retains_project_and_outputs(tmp_path, monkeypatch, failure):
    store = JobStore(tmp_path)
    job = finished_job(store)
    before = snapshot_model(store, job)
    download(store, job, 'elevation.tif', b'complete input')
    def fail_copy(incoming, outgoing):
        outgoing.write(b'partial')
        raise OSError('QA disk failure')
    def fail_replace(*args):
        raise PermissionError('QA target locked')
    if failure == 'copy_partial':
        monkeypatch.setattr(cache.shutil, 'copyfileobj', fail_copy)
    elif failure == 'replace':
        monkeypatch.setattr(cache.os, 'replace', fail_replace)
    else:
        monkeypatch.setattr(cache.os, 'fsync', fail_replace)
    with pytest.raises(OSError):
        store.delete(job['id'], clear_cache=False)
    assert job['id'] in store.jobs
    assert snapshot_model(store, job) == before
    assert job['id'] in JobStore(tmp_path).jobs
    assert not list((tmp_path / 'cache').rglob('*.tmp'))
    assert not shared(store, job['bbox'], 'elevation.tif').exists()


def test_partial_cache_cleanup_failure_during_delete_preserves_all_models(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    target = finished_job(store)
    other = finished_job(store, 'kept')
    before = {job['id']: snapshot_model(store, job) for job in (target, other)}
    for filename in app.DOWNLOADS.values():
        shared(store, target['bbox'], filename).write_bytes(filename.encode())
    original_unlink = Path.unlink
    removed = []
    def fail_second(path, *args, **kwargs):
        if path.is_relative_to(tmp_path / 'cache'):
            if removed:
                raise PermissionError('QA cache file locked')
            removed.append(path)
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_second)
    with pytest.raises(cache.CacheCleanupError) as caught:
        store.delete(target['id'], clear_cache=True)
    assert caught.value.cleared_files == 1
    assert set(store.jobs) == {'finished', 'kept'}
    assert set(JobStore(tmp_path).jobs) == {'finished', 'kept'}
    for job in (target, other):
        assert snapshot_model(store, job) == before[job['id']]


def test_empty_completed_input_rejects_delete_without_touching_other_inputs(tmp_path):
    store = JobStore(tmp_path)
    job = finished_job(store)
    before = snapshot_model(store, job)
    download(store, job, 'elevation.tif', b'complete')
    download(store, job, 'building.geoparquet', b'')
    with pytest.raises(ValueError, match='为空'):
        store.delete(job['id'])
    assert not (tmp_path / 'cache').exists()
    assert snapshot_model(store, job) == before
    assert job['id'] in JobStore(tmp_path).jobs


def test_corrupt_shared_graph_is_not_injected_as_completed_input(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    bbox = [118.77, 32.02, 118.78, 32.03]
    broken = shared(store, bbox, 'street.json')
    broken.write_bytes(b'{"nodes":[')
    config = create_without_worker(store, monkeypatch, bbox)
    assert 'street' not in config.get('address_overrides', {}), 'truncated graph skips OSM redownload'


def test_empty_shared_input_repaired_when_deleting_completed_project(tmp_path):
    store = JobStore(tmp_path)
    job = finished_job(store)
    original = b'complete elevation'
    download(store, job, 'elevation.tif', original)
    target = shared(store, job['bbox'], 'elevation.tif')
    target.write_bytes(b'')
    store.delete(job['id'], clear_cache=False)
    assert target.read_bytes() == original
    assert not store.jobs


def graph_document():
    return dict(directed=True, multigraph=True, graph={'crs': 'EPSG:32650'},
                nodes=[dict(id=1, x=1., y=2.), dict(id=2, x=3., y=4.)],
                links=[dict(source=1, target=2, key=0)])


@pytest.mark.parametrize('malformation', ['wrong_type', 'missing_links', 'missing_nodes',
                                        'bad_flags', 'bad_graph', 'duplicate_node',
                                        'missing_coordinate', 'infinite_coordinate',
                                        'missing_endpoint', 'missing_key', 'bad_geometry'])
def test_structurally_incomplete_graph_cache_is_a_miss(tmp_path, malformation):
    document = graph_document()
    if malformation == 'wrong_type':
        document = []
    elif malformation == 'missing_links':
        del document['links']
    elif malformation == 'missing_nodes':
        del document['nodes']
    elif malformation == 'bad_flags':
        document['multigraph'] = 'true'
    elif malformation == 'bad_graph':
        document['graph'] = []
    elif malformation == 'duplicate_node':
        document['nodes'].append(document['nodes'][0])
    elif malformation == 'missing_coordinate':
        del document['nodes'][0]['x']
    elif malformation == 'infinite_coordinate':
        document['nodes'][0]['x'] = float('inf')
    elif malformation == 'missing_endpoint':
        document['links'][0]['target'] = 99
    elif malformation == 'missing_key':
        del document['links'][0]['key']
    else:
        document['links'][0]['geometry'] = [[1, 2], [3, 4]]
    target = tmp_path / 'street.json'
    target.write_text(json.dumps(document), encoding='utf-8')
    assert not cache.is_usable_download(target, 'street')
    assert target.exists(), 'validation is read-only'


def test_complete_nonempty_street_and_empty_river_are_valid(tmp_path):
    street = tmp_path / 'street.json'
    street.write_text(json.dumps(graph_document()), encoding='utf-8')
    river = tmp_path / 'river.json'
    river.write_text(json.dumps(dict(directed=True, multigraph=True, graph={}, nodes=[], links=[])))
    assert cache.is_usable_download(street, 'street')
    assert cache.is_usable_download(river, 'river')


def test_corrupt_shared_cache_cannot_override_valid_history_or_survive_preservation(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = finished_job(store)
    body = json.dumps(graph_document()).encode()
    source = download(store, job, 'street.json', body)
    target = shared(store, job['bbox'], 'street.json')
    target.write_bytes(b'{"nodes":[')
    config = create_without_worker(store, monkeypatch, job['bbox'])
    assert Path(config['address_overrides']['street']) == source
    store.delete(job['id'], clear_cache=False)
    assert target.read_bytes() == body
    assert cache.is_usable_download(target, 'street')


def test_corrupt_local_history_input_is_not_reused_and_blocks_keep_cache_delete(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = finished_job(store)
    before = snapshot_model(store, job)
    download(store, job, 'elevation.tif', b'complete elevation')
    bad = download(store, job, 'street.json', b'{"nodes":[')
    assert 'street' not in store.download_sources(job['bbox'])
    config = create_without_worker(store, monkeypatch, job['bbox'])
    assert 'street' not in config.get('address_overrides', {})
    with pytest.raises(ValueError, match='损坏.*项目未删除'):
        store.delete(job['id'], clear_cache=False)
    assert snapshot_model(store, job) == before
    assert bad.exists()
    assert not (tmp_path / 'cache').exists(), 'preflight rejects before preserving any other source'


def test_unreadable_graph_falls_back_to_history(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = finished_job(store)
    body = json.dumps(graph_document()).encode()
    source = download(store, job, 'street.json', body)
    target = shared(store, job['bbox'], 'street.json')
    target.write_bytes(body)
    original_open = Path.open
    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError('QA unreadable cache')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    config = create_without_worker(store, monkeypatch, job['bbox'])
    assert Path(config['address_overrides']['street']) == source


@pytest.mark.parametrize('local_body', [b'', b'{"nodes":['])
def test_valid_shared_input_allows_delete_when_redundant_local_copy_is_damaged(tmp_path, local_body):
    store = JobStore(tmp_path)
    job = finished_job(store)
    download(store, job, 'street.json', local_body)
    target = shared(store, job['bbox'], 'street.json')
    body = json.dumps(graph_document()).encode()
    target.write_bytes(body)
    store.delete(job['id'], clear_cache=False)
    assert target.read_bytes() == body
    assert job['id'] not in JobStore(tmp_path).jobs
