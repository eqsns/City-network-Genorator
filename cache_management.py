"""Account for and remove only the application's explicit cache locations.

The caller serializes generation changes and holds MapService.cache_guard when
touching map tiles. Finished models, task state and map position are never cache.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


DOWNLOADS = dict(elevation='elevation.tif', building='building.geoparquet',
                 street='street.json', river='river.json')


def _graph_id(value):
    return (isinstance(value, str) or type(value) is int
            or (type(value) is float and math.isfinite(value)))


def _valid_graph(value):
    """Check the node-link format written by save_graph without importing GIS.

    An empty river graph is a valid completed result. Edges must reference
    existing nodes, because NetworkX otherwise silently creates incomplete
    nodes whose missing coordinates fail much later during generation.
    """
    if not isinstance(value, dict) or not all(type(value.get(key)) is bool
                                             for key in ('directed', 'multigraph')):
        return False
    if not isinstance(value.get('graph'), dict) or not all(
            isinstance(value.get(key), list) for key in ('nodes', 'links')):
        return False
    node_ids = set()
    for node in value['nodes']:
        if not isinstance(node, dict) or not _graph_id(node.get('id')):
            return False
        identity = node['id']
        if identity in node_ids or not all(
                type(node.get(axis)) in (int, float) and math.isfinite(node[axis])
                for axis in ('x', 'y')):
            return False
        node_ids.add(identity)
    for edge in value['links']:
        if not isinstance(edge, dict) or not all(
                _graph_id(edge.get(key)) and edge[key] in node_ids
                for key in ('source', 'target')):
            return False
        if value['multigraph'] and not _graph_id(edge.get('key')):
            return False
        if 'geometry' in edge and not isinstance(edge['geometry'], str):
            return False
    return True


def is_usable_download(path, key):
    """Lightweight reuse check; invalid or unreadable inputs become cache misses.

    Binary GIS inputs must be nonempty. Streets and rivers additionally require
    parseable, structurally complete node-link JSON. Full raster/vector checks
    still belong to the computation worker rather than the local web service.
    """
    if key not in DOWNLOADS:
        raise ValueError('下载完成阶段无效')
    path = Path(path)
    try:
        if not path.is_file() or not path.stat().st_size:
            return False
        if key in ('street', 'river'):
            with path.open('r', encoding='utf-8') as stream:
                return _valid_graph(json.load(stream))
        return True
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        return False


class CacheCleanupError(RuntimeError):
    """A filesystem error after cleanup began; expose the actual partial result."""

    def __init__(self, cause, cleared_bytes, cleared_files):
        self.cleared_bytes = cleared_bytes
        self.cleared_files = cleared_files
        super().__init__(f'缓存清理未全部完成：已删除 {cleared_files} 个文件、'
                         f'{cleared_bytes} 字节。{cause}')


@dataclass(frozen=True)
class _Entry:
    path: Path
    size: int


def _absolute(path):
    return Path(os.path.abspath(path))


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _root(data):
    root = _absolute(data)
    info = root.lstat()
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError('缓存数据根目录无效，不能为链接或重解析点')
    if root.resolve() != root:
        raise ValueError('缓存数据根目录包含链接或重解析路径')
    return root


def _check(root, path, *, directory=None):
    """Check every existing component without following a linked child."""
    path = _absolute(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f'缓存路径超出数据目录：{path}') from exc
    current = root
    info = None
    for part in ('.', *relative.parts):
        if part != '.':
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if _linked(info):
            raise ValueError(f'缓存路径包含链接或重解析点，未清理：{current}')
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f'缓存路径不是文件夹：{current}')
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'缓存路径超出数据目录：{path}')
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise ValueError(f'缓存目录无效：{path}')
    if directory is False and not stat.S_ISREG(info.st_mode):
        raise ValueError(f'缓存文件无效：{path}')
    return info


def _job(root, directory):
    directory = _absolute(directory)
    if directory == root / 'sample':
        _check(root, directory, directory=True)
        return None
    if directory.parent != root / 'runs':
        raise ValueError(f'任务缓存路径无效：{directory}')
    _check(root, directory, directory=True)
    return directory


def _targets(root, runs, generation, map_tiles):
    targets = []
    if generation:
        targets.extend(root / name for name in ('cache', '.cache', 'numba-cache'))
        for supplied in runs:
            job = _job(root, supplied)
            if job is None or not job.exists():
                continue
            targets.append(job / 'national')
            for child in job.iterdir():
                if re.fullmatch(r'bbox_[1-9][0-9]*', child.name):
                    _check(root, child, directory=True)
                    targets.append(child / 'download')
    if map_tiles:
        targets.append(root / 'map-cache')
    return list(dict.fromkeys(targets))


def _scan(root, targets):
    files, directories = [], []

    def visit(path):
        if _check(root, path, directory=True) is None:
            return
        directories.append(path)
        for child in path.iterdir():
            info = _check(root, child)
            if info is None:
                continue
            if stat.S_ISDIR(info.st_mode):
                visit(child)
            elif stat.S_ISREG(info.st_mode):
                files.append(_Entry(child, info.st_size))
            else:
                raise ValueError(f'缓存包含非常规文件，未清理：{child}')

    for path in targets:
        visit(path)
    return files, directories


def inventory(data, runs):
    """Return byte/file totals for known generation caches and map tiles."""
    root, runs = _root(data), tuple(runs)
    result = {}
    for key, generation, maps in (('generation', True, False), ('map', False, True)):
        files, _ = _scan(root, _targets(root, runs, generation, maps))
        result[key] = dict(bytes=sum(item.size for item in files), files=len(files))
    return result


def clear(data, runs, generation=True, map=False):
    """Preflight every selected cache, then remove it without following links."""
    if type(generation) is not bool or type(map) is not bool:
        raise ValueError('缓存清理选项必须为布尔值')
    root = _root(data)
    files, directories = _scan(root, _targets(root, runs, generation, map))
    cleared_bytes = cleared_files = 0
    try:
        for item in files:
            info = _check(root, item.path, directory=False)
            if info is not None:
                item.path.unlink()
                cleared_bytes += info.st_size
                cleared_files += 1
        for path in reversed(directories):
            if _check(root, path, directory=True) is not None:
                path.rmdir()
    except (OSError, ValueError) as exc:
        raise CacheCleanupError(exc, cleared_bytes, cleared_files) from exc
    return dict(cleared_bytes=cleared_bytes, cleared_files=cleared_files)


def _atomic_copy(root, source, destination, key):
    temporary = None
    try:
        _check(root, destination.parent, directory=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent,
                prefix=f'.{destination.name}.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            _check(root, source, directory=False)
            with source.open('rb') as incoming:
                shutil.copyfileobj(incoming, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size != source.stat().st_size:
            raise OSError(f'缓存复制不完整：{source.name}')
        if not is_usable_download(temporary, key):
            raise ValueError(f'下载文件损坏或复制不完整，项目未删除：{source.name}')
        _check(root, destination, directory=False)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def preserve_downloads(data, job_dir, bbox, completed_keys):
    """Keep verified inputs before removing a project that keeps its cache.

    Missing local inputs may already be shared through config overrides. Never
    promote partial phases just because a file exists. Valid shared inputs are
    kept; corrupt caches are replaced only from a valid completed local input.
    Failed validation/copies propagate so the caller keeps the project.
    """
    root = _root(data)
    job = _job(root, job_dir)
    if job is None:
        return dict(preserved_bytes=0, preserved_files=0)
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in bbox):
        raise ValueError('缓存范围坐标无效')
    keys = tuple(dict.fromkeys(completed_keys))
    if any(key not in DOWNLOADS for key in keys):
        raise ValueError('下载完成阶段无效')
    cached = root / 'cache' / hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
    _check(root, cached, directory=True)
    pending = []
    for key in keys:
        source = job / 'bbox_1' / 'download' / DOWNLOADS[key]
        destination = cached / DOWNLOADS[key]
        source_info = _check(root, source, directory=False)
        destination_info = _check(root, destination, directory=False)
        if destination_info is not None and is_usable_download(destination, key):
            continue
        if source_info is not None:
            if not source_info.st_size:
                raise ValueError(f'已完成的下载文件为空，项目未删除：{source.name}')
            if not is_usable_download(source, key):
                raise ValueError(f'已完成的下载文件损坏或无法读取，项目未删除：{source.name}')
            pending.append((source, destination, source_info.st_size, key))
    if pending:
        cached.mkdir(parents=True, exist_ok=True)
    total = 0
    for source, destination, size, key in pending:
        _atomic_copy(root, source, destination, key)
        total += size
    return dict(preserved_bytes=total, preserved_files=len(pending))
