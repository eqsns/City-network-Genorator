import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cache_management as cache
from map_service import MapService


def put(root, relative, content=b'cache'):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def populated(tmp_path):
    cached = [put(tmp_path, name, name.encode()) for name in (
        'cache/request.json', 'cache/area/elevation.tif', '.cache/overture_release.json',
        'numba-cache/module/compiled.nbc', 'runs/one/bbox_1/download/street.json',
        'runs/one/bbox_2/download/elevation.tif', 'runs/one/national/building.geoparquet',
        'runs/two/bbox_1/download/river.json')]
    preserved = [put(tmp_path, name) for name in (
        'map-view.json', 'settings.json', 'network.log', 'runs/one/app_job.json',
        'runs/one/run.log', 'runs/one/events.jsonl', 'runs/one/config.yml',
        'runs/one/bbox_1/model_1/model_1.inp', 'runs/one/bbox_1/model_1/network.geojson',
        'runs/two/bbox_1/model_1/SWMM-model.zip', 'sample/network.geojson',
        'unknown/bbox_1/download/street.json', 'runs/unregistered/bbox_1/download/street.json')]
    tiles = [put(tmp_path, name) for name in ('map-cache/1/0/0.png', 'map-cache/1/0/0.json')]
    runs = [tmp_path / 'runs' / 'one', tmp_path / 'runs' / 'two', tmp_path / 'sample']
    return cached, preserved, tiles, runs


def test_inventory_and_generation_clear_preserve_models_position_unknown_runs_and_map(tmp_path):
    cached, preserved, tiles, runs = populated(tmp_path)
    size = sum(path.stat().st_size for path in cached)
    assert cache.inventory(tmp_path, runs) == {
        'generation': {'bytes': size, 'files': len(cached)},
        'map': {'bytes': 10, 'files': 2}}
    assert cache.clear(tmp_path, runs) == {'cleared_bytes': size, 'cleared_files': len(cached)}
    assert all(not path.exists() for path in cached)
    assert all(path.exists() for path in preserved + tiles)
    assert cache.inventory(tmp_path, runs)['generation'] == {'bytes': 0, 'files': 0}


def test_clear_all_selected_caches_and_repeat(tmp_path):
    cached, preserved, tiles, runs = populated(tmp_path)
    expected = sum(path.stat().st_size for path in cached + tiles)
    assert cache.clear(tmp_path, runs, map=True) == {
        'cleared_bytes': expected, 'cleared_files': len(cached + tiles)}
    assert all(path.exists() for path in preserved)
    assert cache.clear(tmp_path, runs, map=True) == {'cleared_bytes': 0, 'cleared_files': 0}


def test_map_only_does_not_remove_downloads(tmp_path):
    cached, preserved, tiles, runs = populated(tmp_path)
    assert cache.clear(tmp_path, runs, generation=False, map=True)['cleared_files'] == len(tiles)
    assert all(path.exists() for path in cached + preserved)


@pytest.mark.parametrize('relative', ['runs', 'cache', 'unknown', 'runs/one/bbox_1', '../outside'])
def test_unknown_job_roots_fail_before_any_delete(tmp_path, relative):
    ordinary = put(tmp_path, 'cache/good.json')
    with pytest.raises(ValueError, match='任务缓存路径无效'):
        cache.clear(tmp_path, [tmp_path / relative])
    assert ordinary.read_bytes() == b'cache'


@pytest.mark.parametrize('directory', [False, True])
def test_symlink_outside_data_prevents_all_cleanup(tmp_path, directory):
    root = tmp_path / 'data'
    ordinary = put(root, 'cache/good.json')
    outside = tmp_path / 'outside'
    target = put(outside, 'keep.txt', b'important')
    link = root / 'cache' / 'linked'
    try:
        link.symlink_to(outside if directory else target, target_is_directory=directory)
    except OSError:
        pytest.skip('Creating symlinks requires Windows developer mode or privilege')
    with pytest.raises(ValueError, match='链接或重解析'):
        cache.clear(root, [], map=True)
    assert ordinary.read_bytes() == b'cache'
    assert target.read_bytes() == b'important'


@pytest.mark.parametrize('is_directory', [False, True])
def test_reparse_is_rejected_even_when_resolves_inside_data(tmp_path, monkeypatch, is_directory):
    ordinary = put(tmp_path, 'cache/good.json')
    reparse = put(tmp_path, 'map-cache/reparse.png')
    if is_directory:
        reparse.unlink()
        reparse.mkdir()
    original = Path.lstat
    def flagged(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == reparse:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', flagged)
    with pytest.raises(ValueError, match='链接或重解析'):
        cache.clear(tmp_path, [], map=True)
    assert ordinary.exists() and reparse.exists()


def test_delete_failure_reports_actual_partial_result(tmp_path, monkeypatch):
    put(tmp_path, 'cache/a.json', b'first')
    put(tmp_path, 'cache/b.json', b'second')
    original = Path.unlink
    removed = []
    def locked(path, *args, **kwargs):
        if removed:
            raise PermissionError('locked test file')
        removed.append((path, path.stat().st_size))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', locked)
    with pytest.raises(cache.CacheCleanupError, match='未全部完成') as error:
        cache.clear(tmp_path, [])
    assert error.value.cleared_files == 1
    assert error.value.cleared_bytes == removed[0][1]
    assert len(list((tmp_path/'cache').iterdir())) == 1


def shared(root, bbox, filename):
    key = hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
    return root / 'cache' / key / filename


def test_preserve_only_confirmed_complete_stages_before_project_delete(tmp_path):
    bbox = [120., 30., 120.1, 30.1]
    job = tmp_path / 'runs' / 'one'
    elevation = put(job, 'bbox_1/download/elevation.tif', b'complete elevation')
    put(job, 'bbox_1/download/building.geoparquet', b'partial building')
    result = cache.preserve_downloads(tmp_path, job, bbox, ['elevation'])
    assert result == {'preserved_files': 1, 'preserved_bytes': elevation.stat().st_size}
    assert shared(tmp_path, bbox, 'elevation.tif').read_bytes() == elevation.read_bytes()
    assert not shared(tmp_path, bbox, 'building.geoparquet').exists()
    assert elevation.exists()


def test_preserve_keeps_existing_nonempty_shared_cache(tmp_path):
    bbox = [120, 30, 120.1, 30.1]
    job = tmp_path / 'runs' / 'one'
    put(job, 'bbox_1/download/elevation.tif', b'new download')
    target = shared(tmp_path, bbox, 'elevation.tif')
    put(target.parent, target.name, b'verified existing')
    assert cache.preserve_downloads(tmp_path, job, bbox, ['elevation'])['preserved_files'] == 0
    assert target.read_bytes() == b'verified existing'


@pytest.mark.parametrize('failure', ['copy', 'replace'])
def test_preserve_failure_leaves_project_and_does_not_publish_partial_cache(tmp_path, monkeypatch, failure):
    bbox = [120, 30, 120.1, 30.1]
    job = tmp_path / 'runs' / 'one'
    source = put(job, 'bbox_1/download/elevation.tif', b'complete')
    def fail(*args, **kwargs):
        raise OSError('disk failure')
    if failure == 'copy':
        monkeypatch.setattr(cache.shutil, 'copyfileobj', fail)
    else:
        monkeypatch.setattr(cache.os, 'replace', fail)
    with pytest.raises(OSError, match='disk failure'):
        cache.preserve_downloads(tmp_path, job, bbox, ['elevation'])
    target = shared(tmp_path, bbox, 'elevation.tif')
    assert not target.exists()
    assert not list(target.parent.glob('*.tmp'))
    assert source.read_bytes() == b'complete'


def test_empty_completed_download_does_not_become_cache(tmp_path):
    job = tmp_path / 'runs' / 'one'
    put(job, 'bbox_1/download/elevation.tif', b'')
    with pytest.raises(ValueError, match='为空'):
        cache.preserve_downloads(tmp_path, job, [120, 30, 121, 31], ['elevation'])
    assert not (tmp_path / 'cache').exists()


def test_preserve_preflights_all_inputs_before_copying(tmp_path, monkeypatch):
    job = tmp_path / 'runs' / 'one'
    elevation = put(job, 'bbox_1/download/elevation.tif', b'complete elevation')
    reparse = put(job, 'bbox_1/download/building.geoparquet', b'linked building')
    original = Path.lstat
    def flagged(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == reparse:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', flagged)
    with pytest.raises(ValueError, match='链接或重解析'):
        cache.preserve_downloads(tmp_path, job, [120, 30, 121, 31], ['elevation', 'building'])
    assert elevation.exists() and reparse.exists()
    assert not (tmp_path/'cache').exists()


def test_inventory_tolerates_tile_temp_disappearing_before_stat(tmp_path, monkeypatch):
    put(tmp_path, 'map-cache/1/0/0.png', b'tile')
    temporary = put(tmp_path, 'map-cache/1/0/0.tmp', b'pending')
    original = Path.lstat
    def disappeared(path, *args, **kwargs):
        if path == temporary:
            raise FileNotFoundError(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', disappeared)
    assert cache.inventory(tmp_path, [])['map'] == {'bytes': 4, 'files': 1}


def test_missing_local_download_and_sample_need_no_copy(tmp_path):
    job = tmp_path / 'runs' / 'one'
    job.mkdir(parents=True)
    assert cache.preserve_downloads(tmp_path, job, [120, 30, 121, 31], ['elevation'])['preserved_files'] == 0
    assert cache.preserve_downloads(tmp_path, tmp_path/'sample', None, [])['preserved_files'] == 0
    assert not (tmp_path/'cache').exists()


def test_tile_guard_releases_all_locks_after_timeout_and_exception(tmp_path):
    service = MapService(tmp_path)
    service.tiles_lock[1].acquire()
    try:
        with pytest.raises(RuntimeError, match='底图仍在加载'):
            with service.cache_guard(timeout=.01):
                pytest.fail('guard must not enter while a tile write holds its lock')
        assert service.tiles_lock[0].acquire(blocking=False)
        service.tiles_lock[0].release()
    finally:
        service.tiles_lock[1].release()
    with pytest.raises(RuntimeError, match='inside cleanup'):
        with service.cache_guard():
            assert all(lock.locked() for lock in service.tiles_lock)
            raise RuntimeError('inside cleanup')
    assert not any(lock.locked() for lock in service.tiles_lock)


def test_clear_selection_requires_real_booleans(tmp_path):
    with pytest.raises(ValueError, match='布尔值'):
        cache.clear(tmp_path, [], generation='false')
