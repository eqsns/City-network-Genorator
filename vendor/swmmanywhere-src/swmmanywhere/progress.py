"""Optional structured progress reporting; no effect for ordinary CLI callers."""
from contextlib import contextmanager
from contextvars import ContextVar
import time


_execution_hooks = ContextVar('execution_hooks', default=(None, None))


@contextmanager
def execution_hooks(checkpoint_callback=None, paused_seconds=None):
    """Install worker-only control hooks without changing command-line use.

    ``paused_seconds`` is a callable returning the accumulated paused duration.
    Context-local hooks intentionally do not pause background HTTP threads.
    """
    token = _execution_hooks.set((checkpoint_callback, paused_seconds))
    try:
        yield
    finally:
        _execution_hooks.reset(token)


def checkpoint():
    if callback := _execution_hooks.get()[0]:
        callback()


def active_monotonic(clock=None):
    """Measure running time so a user pause never consumes a download budget."""
    offset = _execution_hooks.get()[1]
    return (clock or time.monotonic)() - (offset() if offset else 0.0)


GRAPH_LABELS = {
    'assign_id': '整理节点与管段编号', 'fix_geometries': '校正管段几何',
    'remove_non_pipe_allowable_links': '筛选可布管道路',
    'calculate_streetcover': '计算道路覆盖面积', 'remove_parallel_edges': '整理重复道路',
    'to_undirected': '整理道路连接', 'split_long_edges': '划分长管段',
    'merge_street_nodes': '合并邻近节点', 'clip_to_catchments': '划分地形汇水区',
    'calculate_contributing_area': '计算汇水面积', 'set_elevation': '提取节点高程',
    'double_directed': '建立管段方向', 'set_surface_slope': '计算地表坡度',
    'set_chahinian_slope': '计算坡度权重', 'set_chahinian_angle': '计算转角权重',
    'calculate_weights': '计算管网权重', 'identify_outfalls': '识别排口',
    'derive_topology': '推导排水管网拓扑', 'pipe_by_pipe': '计算管径与埋深',
}


def report(callback, phase, percent, message, *, checkpoint=True):
    if checkpoint and (safe_point := _execution_hooks.get()[0]):
        safe_point()
    if callback is not None:
        callback(phase, percent, message)
