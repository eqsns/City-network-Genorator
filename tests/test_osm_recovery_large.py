"""Deterministic regressions for large-area Overpass recovery and budgets.

No public endpoint, real sleep, thread, VPN setting, or production cache is
touched: the clock and single-endpoint transport are owned by each test.
"""
from collections import Counter
from queue import Queue
from threading import Event
from types import SimpleNamespace
import json

import bootstrap  # noqa: F401: locate the application's vendored engine
import pytest
import requests

from swmmanywhere import osm_network as net


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []
        self.reads = 0

    def monotonic(self):
        self.reads += 1
        assert self.reads < 5000, 'Recovery is busy-looping without advancing time'
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= net.HEARTBEAT_SECONDS
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def controlled(monkeypatch):
    clock = Clock()
    saved, notices = [], []
    client = net.OverpassClient()
    monkeypatch.setattr(net, 'time', clock)
    monkeypatch.setattr(net, 'client', client)
    monkeypatch.setattr(net, 'notice', notices.append)
    monkeypatch.setattr(net.settings, 'overpass_url', 'https://overpass-api.de/api')
    monkeypatch.setattr(net.osm_cache, 'retrieve', lambda key: None)
    monkeypatch.setattr(net.osm_cache, 'save', lambda *args: (saved.append(args), True)[1])
    return SimpleNamespace(clock=clock, client=client, saved=saved, notices=notices,
                           main=net.settings.overpass_url, mirrors=net.MIRRORS)


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f'HTTP {status}', response=response)


@pytest.mark.parametrize('status', [429, 504])
def test_temporary_http_failure_recovers_once_after_cooldown(controlled, monkeypatch, status):
    state = controlled
    attempts = []

    def attempt(base, data, deadline):
        attempts.append((base, state.clock.now, deadline))
        if base != state.main:
            raise http_error(403)
        if sum(base == row[0] for row in attempts) == 1:
            raise net.EndpointUnavailable(f'HTTP {status}', cooldown=35)
        return {'elements': [{'type': 'node', 'id': 42}]}

    monkeypatch.setattr(state.client, '_attempt', attempt)
    assert state.client.request({'data': 'tile-10'})['elements'][0]['id'] == 42
    assert [row[0] for row in attempts] == [state.main, *state.mirrors, state.main]
    assert attempts[-1][1] == 135.0
    assert sum(state.clock.waits) == 35
    assert {row[2] for row in attempts} == {100.0 + net.QUERY_SECONDS}
    assert len(state.saved) == 1


@pytest.mark.parametrize('status', [429, 504])
def test_retry_after_longer_than_remaining_budget_is_not_waited_or_retried(controlled, monkeypatch, status):
    """Exercise Retry-After parsing through the real _fetch HTTP path."""
    state = controlled
    calls = []
    monkeypatch.setattr(net.settings, 'overpass_rate_limit', False)

    class Session:
        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            calls.append((url, state.clock.now))
            response = requests.Response()
            response.status_code = status
            response.headers['Retry-After'] = '600'
            response._content = json.dumps({'elements': []}).encode()
            response._content_consumed = True
            return response

        post = get

    monkeypatch.setattr(net.requests, 'Session', Session)
    monkeypatch.setattr(state.client, '_attempt',
                        lambda base, data, deadline: state.client._fetch(base, data, Queue(), Event()))
    with pytest.raises(requests.ConnectionError):
        state.client.request({'data': '[out:json];node(1);out;'})
    assert len(calls) == len(set(url for url, _ in calls)) == 3
    assert not state.clock.waits
    assert not state.saved
    assert all(end >= 700 for end in state.client.cooldown.values())


def test_timeout_cooldown_survives_tile_22_and_recovers_in_tile_23(controlled, monkeypatch):
    """Reproduce the 2026-10-07 failure after 22 successful road pieces."""
    state = controlled
    mirror, coffee = state.mirrors
    attempts = []

    def attempt(base, data, deadline):
        tile = data['data']
        attempts.append((tile, base, state.clock.now, deadline))
        if tile == 'road-tile-22':
            if base == state.main:
                state.clock.now += 70
                raise requests.ReadTimeout('response timed out')
            assert base == mirror
            state.clock.now += 10
            return {'elements': [{'type': 'way', 'id': 22}]}
        if base == mirror:
            raise net.EndpointUnavailable('HTTP 504', cooldown=60)
        if base == coffee:
            state.clock.now += 70
            raise requests.ReadTimeout('response timed out')
        return {'elements': [{'type': 'way', 'id': 23}]}

    monkeypatch.setattr(state.client, '_attempt', attempt)
    assert state.client.request({'data': 'road-tile-22'})['elements'][0]['id'] == 22
    assert state.client.cooldown[state.main] == 230
    assert state.client.working == mirror
    assert state.client.request({'data': 'road-tile-23'})['elements'][0]['id'] == 23
    assert [row[:3] for row in attempts] == [
        ('road-tile-22', state.main, 100.0),
        ('road-tile-22', mirror, 170.0),
        ('road-tile-23', mirror, 180.0),
        ('road-tile-23', coffee, 180.0),
        ('road-tile-23', state.main, 250.0),
    ]
    assert {row[3] for row in attempts if row[0] == 'road-tile-23'} == {180.0 + net.QUERY_SECONDS}
    assert state.client.working == state.main
    assert state.main not in state.client.cooldown
    assert not state.clock.waits
    assert len(state.saved) == 2


@pytest.mark.parametrize('error', ['timeout', 'ssl_eof'])
def test_all_transient_failures_are_bounded_per_endpoint_and_never_cached(controlled, monkeypatch, error):
    state = controlled
    attempts = []

    def attempt(base, data, deadline):
        attempts.append((base, state.clock.now, deadline))
        if error == 'ssl_eof':
            raise requests.exceptions.SSLError('[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol')
        raise requests.ConnectTimeout('connect timed out')

    monkeypatch.setattr(state.client, '_attempt', attempt)
    with pytest.raises(requests.ConnectionError):
        state.client.request({'data': 'unavailable-tile'})
    counts = Counter(row[0] for row in attempts)
    assert set(counts) == {state.main, *state.mirrors}
    assert all(count == 2 for count in counts.values())
    assert {row[2] for row in attempts} == {100.0 + net.QUERY_SECONDS}
    assert sum(state.clock.waits) == 60
    assert state.clock.now < 100.0 + net.QUERY_SECONDS
    assert not state.saved


@pytest.mark.parametrize('error', ['forbidden', 'certificate'])
def test_permanent_failure_is_not_retried_even_with_time_remaining(controlled, monkeypatch, error):
    state = controlled
    attempts = []

    def attempt(base, data, deadline):
        attempts.append(base)
        if error == 'certificate':
            raise requests.exceptions.SSLError('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')
        raise http_error(403)

    monkeypatch.setattr(state.client, '_attempt', attempt)
    with pytest.raises(requests.ConnectionError):
        state.client.request({'data': 'permanent-failure'})
    assert attempts == [state.main, *state.mirrors]
    assert not state.clock.waits
    assert not state.saved


@pytest.mark.parametrize('bound', ['query', 'stage'])
def test_recovery_cannot_cross_original_deadline(controlled, monkeypatch, bound):
    state = controlled
    if bound == 'query':
        monkeypatch.setattr(net, 'QUERY_SECONDS', 75)
    else:
        state.client.stage_deadline = 175.0
    attempts = []

    def attempt(base, data, deadline):
        assert state.clock.now < deadline
        attempts.append((base, state.clock.now, deadline))
        state.clock.now = min(state.clock.now + 60, deadline)
        raise requests.ReadTimeout('slow response')

    monkeypatch.setattr(state.client, '_attempt', attempt)
    with pytest.raises(requests.ConnectionError):
        state.client.request({'data': 'slow-tile'})
    assert attempts == [(state.main, 100.0, 175.0), (state.mirrors[0], 160.0, 175.0)]
    assert state.clock.now == 175.0
    assert not state.clock.waits
    assert not state.saved


@pytest.mark.parametrize('bound', ['query', 'stage'])
def test_cooldown_ending_at_deadline_cannot_trigger_recovery(controlled, monkeypatch, bound):
    state = controlled
    if bound == 'query':
        monkeypatch.setattr(net, 'QUERY_SECONDS', 60)
    else:
        state.client.stage_deadline = 160.0
    attempts = []

    def attempt(base, data, deadline):
        attempts.append(base)
        if base == state.main:
            raise net.EndpointUnavailable('HTTP 429', cooldown=60)
        raise http_error(403)

    monkeypatch.setattr(state.client, '_attempt', attempt)
    with pytest.raises(requests.ConnectionError):
        state.client.request({'data': 'cooldown-outlives-budget'})
    assert attempts == [state.main, *state.mirrors]
    assert not state.clock.waits
    assert not state.saved


@pytest.mark.parametrize('query_count,seconds', [(1, 480), (8, 480), (9, 540), (49, 2940), (100, 3600)])
def test_workload_budget_is_fixed_to_stage_start(controlled, query_count, seconds):
    state = controlled
    with net.bounded_requests():
        assert state.client.stage_deadline == 580.0
        state.clock.now = 120.0
        net.configure_workload(query_count)
        assert state.client.stage_deadline == 100.0 + seconds
        state.clock.now = 130.0
        net.configure_workload(1000)
        assert state.client.stage_deadline == 100.0 + seconds
    assert state.client.stage_deadline is None


def test_nested_budget_cannot_extend_parent_and_restores_outer_configuration(controlled):
    state = controlled
    with net.bounded_requests():
        net.configure_workload(9)
        assert state.client.stage_deadline == 640.0
        state.clock.now = 120.0
        with net.bounded_requests():
            net.configure_workload(100)
            assert state.client.stage_deadline == 640.0
        assert state.client.stage_deadline == 640.0
        state.clock.now = 140.0
        net.configure_workload(100)
        assert state.client.stage_deadline == 640.0
    assert state.client.stage_deadline is None
    state.clock.now = 200.0
    with net.bounded_requests():
        net.configure_workload(49)
        assert state.client.stage_deadline == 3140.0


def test_large_budget_still_caps_each_query_and_honors_remaining_stage(controlled, monkeypatch):
    state = controlled
    attempts = []

    def attempt(base, data, deadline):
        attempts.append((data['data'], deadline))
        raise http_error(403)

    monkeypatch.setattr(state.client, '_attempt', attempt)
    with net.bounded_requests():
        net.configure_workload(49)
        with pytest.raises(requests.ConnectionError):
            state.client.request({'data': 'early-tile'})
        state.clock.now = 2900.0
        with pytest.raises(requests.ConnectionError):
            state.client.request({'data': 'late-tile'})
        assert state.client.stage_deadline == 3040.0
    assert {deadline for tile, deadline in attempts if tile == 'early-tile'} == {340.0}
    assert {deadline for tile, deadline in attempts if tile == 'late-tile'} == {3040.0}
    assert not state.saved
    assert not state.clock.waits
