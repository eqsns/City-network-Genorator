"""Cooperative worker control and download budgets, without network access."""
import json

import pytest

import bootstrap
from execution_control import WorkerControl
from swmmanywhere import osm_network, preprocessing, progress


def command(path, action, revision):
    path.write_text(json.dumps({'action': action, 'revision': revision}), encoding='utf-8')


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def test_no_command_is_cli_compatible_and_resume_acknowledges_withdrawal(tmp_path):
    path, events = tmp_path / 'control.json', []
    control = WorkerControl(path, events.append)
    control.checkpoint()
    assert events == []
    assert control.paused_seconds == 0
    command(path, 'resume', 2)
    control.checkpoint()
    control.checkpoint()
    assert events == [{'type': 'control', 'state': 'running', 'revision': 2, 'paused_seconds': 0}]


def test_pause_revisions_missing_file_and_stale_resume_do_not_release_worker(tmp_path):
    path, events, clock = tmp_path / 'control.json', [], Clock()
    command(path, 'pause', 1)
    actions = iter([
        lambda: command(path, 'pause', 2),
        lambda: path.unlink(),
        lambda: command(path, 'resume', 1),
        lambda: command(path, 'resume', 3),
    ])
    def sleep(_):
        clock.now += 4
        next(actions)()
    control = WorkerControl(path, events.append, clock=clock, sleep=sleep)
    control.checkpoint()
    assert [(e['state'], e['revision']) for e in events] == [('paused', 1), ('paused', 2), ('running', 3)]
    assert events[0]['paused_at'] == events[1]['paused_at']
    assert [e['paused_seconds'] for e in events] == [0, 0, 16]
    clock.now += 10
    assert control.paused_seconds == 16


def test_unobserved_resume_then_pause_keeps_one_pause_interval(tmp_path):
    path, events, clock = tmp_path / 'control.json', [], Clock()
    command(path, 'pause', 1)
    def overwritten_resume():
        command(path, 'resume', 2)
        command(path, 'pause', 3)
    actions = iter([overwritten_resume, lambda: command(path, 'resume', 4)])
    def sleep(_):
        clock.now += 5
        next(actions)()
    control = WorkerControl(path, events.append, clock=clock, sleep=sleep)
    control.checkpoint()
    assert [(e['state'], e['revision']) for e in events] == [('paused', 1), ('paused', 3), ('running', 4)]
    assert events[0]['paused_at'] == events[1]['paused_at']
    # The supervisor adds the current interval using paused_at. Never include
    # this interval in paused_seconds until the worker acknowledges running.
    assert [e['paused_seconds'] for e in events] == [0, 0, 10]


def test_multiple_pauses_accumulate_without_counting_running_time(tmp_path):
    path, events, clock = tmp_path / 'control.json', [], Clock()
    def sleep(_):
        clock.now += 7
        command(path, 'resume', json.loads(path.read_text())['revision'] + 1)
    control = WorkerControl(path, events.append, clock=clock, sleep=sleep)
    command(path, 'pause', 1)
    control.checkpoint()
    clock.now += 20
    command(path, 'pause', 3)
    control.checkpoint()
    assert control.paused_seconds == 14
    assert [e['paused_seconds'] for e in events] == [0, 7, 7, 14]


@pytest.mark.parametrize('content', ['broken{', 'null', '[]', '{}', '{"action":"stop","revision":1}',
                                      '{"action":"resume","revision":true}',
                                      '{"action":"resume","revision":-1}'])
def test_invalid_commands_fail_explicitly(tmp_path, content):
    path = tmp_path / 'control.json'
    path.write_text(content)
    with pytest.raises(RuntimeError, match='命令损坏'):
        WorkerControl(path, lambda event: None).checkpoint()


def test_bad_command_while_paused_never_emits_running(tmp_path):
    path, events = tmp_path / 'control.json', []
    command(path, 'pause', 1)
    control = WorkerControl(path, events.append, sleep=lambda _: path.write_text('bad JSON'))
    with pytest.raises(RuntimeError, match='命令损坏'):
        control.checkpoint()
    assert [e['state'] for e in events] == ['paused']


def test_unreadable_command_while_paused_fails_without_resuming(tmp_path, monkeypatch):
    path, events = tmp_path / 'control.json', []
    command(path, 'pause', 1)
    def denied(*args, **kwargs):
        raise PermissionError('locked')
    def sleep(_):
        monkeypatch.setattr(type(path), 'read_text', denied)
    control = WorkerControl(path, events.append, sleep=sleep)
    with pytest.raises(RuntimeError, match='无法读取暂停/恢复命令'):
        control.checkpoint()
    assert [e['state'] for e in events] == ['paused']


def test_conflicting_revision_cannot_resume(tmp_path):
    path, events = tmp_path / 'control.json', []
    command(path, 'pause', 1)
    control = WorkerControl(path, events.append, sleep=lambda _: command(path, 'resume', 1))
    with pytest.raises(RuntimeError, match='版本冲突'):
        control.checkpoint()
    assert [e['state'] for e in events] == ['paused']


def test_progress_hooks_are_optional_and_restore_after_errors():
    checkpoints, notices = [], []
    callback = lambda *args: notices.append(args)
    progress.report(callback, 'graph', 45, 'normal')
    assert progress.active_monotonic(lambda: 200) == 200
    with progress.execution_hooks(lambda: checkpoints.append('outer'), lambda: 10):
        progress.report(callback, 'graph', 46, 'safe point')
        progress.report(callback, 'street', 25, 'HTTP heartbeat', checkpoint=False)
        progress.report(None, 'graph', 47, 'no UI callback')
        assert progress.active_monotonic(lambda: 200) == 190
        with pytest.raises(ValueError):
            with progress.execution_hooks(lambda: checkpoints.append('inner'), lambda: 20):
                progress.checkpoint()
                assert progress.active_monotonic(lambda: 200) == 180
                raise ValueError('stop nested scope')
        progress.checkpoint()
    progress.checkpoint()
    assert progress.active_monotonic(lambda: 200) == 200
    assert checkpoints == ['outer', 'outer', 'inner', 'outer']
    assert len(notices) == 3


def test_osm_heartbeat_does_not_pause_inflight_download(tmp_path, monkeypatch):
    from types import SimpleNamespace
    paths = SimpleNamespace(bbox_paths=SimpleNamespace(**{
        name: tmp_path / name for name in ('elevation', 'building', 'street', 'river')}))
    checkpoints, notices = [], []
    def prepare(*args, **kwargs):
        osm_network.notice('HTTP request still active')
    for name in ('elevation', 'building', 'street', 'river'):
        monkeypatch.setattr(preprocessing, 'prepare_' + name, prepare)
    with progress.execution_hooks(lambda: checkpoints.append('safe')):
        preprocessing.run_downloads([118.77, 32.02, 118.78, 32.03], paths,
                                    progress_callback=lambda *args: notices.append(args))
    assert len(checkpoints) == 5  # Four stage boundaries and the completed download boundary.
    assert len(notices) == 9  # Four in-flight notices bypass the pause hook.


def test_osm_query_and_stage_budgets_exclude_long_pause(tmp_path, monkeypatch):
    path, events, clock = tmp_path / 'control.json', [], Clock()
    def sleep(_):
        clock.now += 3600
        command(path, 'resume', 2)
    control = WorkerControl(path, events.append, clock=clock, sleep=sleep)
    transport = osm_network.OverpassClient()
    monkeypatch.setattr(osm_network, 'client', transport)
    monkeypatch.setattr(osm_network.time, 'monotonic', clock)
    monkeypatch.setattr(osm_network.settings, 'overpass_url', 'https://example.test/api')
    monkeypatch.setattr(osm_network, 'MIRRORS', ())
    monkeypatch.setattr(osm_network.osm_cache, 'retrieve', lambda key: None)
    monkeypatch.setattr(osm_network.osm_cache, 'save', lambda key, data: True)
    attempts = []
    def attempt(base, data, deadline):
        attempts.append((clock(), deadline, transport.stage_deadline))
        clock.now += 5
        return {'elements': []}
    monkeypatch.setattr(transport, '_attempt', attempt)
    with progress.execution_hooks(control.checkpoint, lambda: control.paused_seconds):
        with osm_network.bounded_requests():
            command(path, 'pause', 1)
            assert transport.request({'data': '[out:json];node(1);out;'}) == {'elements': []}
            assert osm_network._active_time() == 105
            assert transport.stage_deadline == 100 + osm_network.STAGE_SECONDS
    assert attempts == [(3700, 100 + osm_network.QUERY_SECONDS, 100 + osm_network.STAGE_SECONDS)]
    assert [e['state'] for e in events] == ['paused', 'running']
    assert control.paused_seconds == 3600


def test_osm_default_active_clock_keeps_existing_clock_monkeypatch(monkeypatch):
    monkeypatch.setattr(osm_network.time, 'monotonic', lambda: 123)
    assert osm_network._active_time() == 123
