"""Cooperative worker pause/resume commands, independent of the desktop app."""
from __future__ import annotations

import json
from pathlib import Path
import time


class WorkerControl:
    """Wait only at explicit safe points; never suspend native work mid-call.

    The supervisor publishes atomic JSON commands with increasing revisions.
    Missing commands mean normal execution on startup. Once pause is observed,
    removing the file cannot resume work: an explicit newer resume is required.
    """

    def __init__(self, control_path, send, *, poll_interval=0.1, clock=None, sleep=None):
        self.control_path = Path(control_path)
        self.send = send
        self.poll_interval = poll_interval
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._revision = -1
        self._action = 'resume'
        self._ack_revision = -1
        self._paused_total = 0.0
        self._pause_started = None
        self._paused_at = None

    @property
    def paused_seconds(self):
        current = 0.0 if self._pause_started is None else self._clock() - self._pause_started
        return self._paused_total + max(0.0, current)

    def _read_command(self):
        # Atomic replacement or a short-lived Windows sharing lock can deny a
        # read even though the command remains valid. Retry only these bounded
        # transient failures; persistent/unrelated errors still fail explicitly.
        delays = (.02, .04, .08, .16, .32)
        for attempt in range(len(delays) + 1):
            try:
                raw = self.control_path.read_text(encoding='utf-8')
                break
            except FileNotFoundError:
                return
            except OSError as exc:
                transient = isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in {5, 32, 33}
                if not transient or attempt == len(delays):
                    raise RuntimeError(f'无法读取暂停/恢复命令：{exc}') from exc
                self._sleep(delays[attempt])
        try:
            command = json.loads(raw)
            action = command['action']
            revision = command['revision']
            if action not in ('pause', 'resume') or type(revision) is not int or revision < 0:
                raise ValueError('命令必须包含 pause/resume 和非负整数 revision')
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f'暂停/恢复命令损坏：{exc}') from exc
        if revision < self._revision:
            return
        if revision == self._revision and action != self._action:
            raise RuntimeError('暂停/恢复命令版本冲突')
        self._revision = revision
        self._action = action

    def checkpoint(self):
        self._read_command()
        while self._action == 'pause':
            if self._pause_started is None:
                self._pause_started = self._clock()
                self._paused_at = time.time()
            if self._ack_revision != self._revision:
                self.send({'type': 'control', 'state': 'paused', 'revision': self._revision,
                           'paused_at': self._paused_at, 'paused_seconds': self._paused_total})
                self._ack_revision = self._revision
            self._sleep(self.poll_interval)
            self._read_command()
        if self._pause_started is not None:
            self._paused_total = self.paused_seconds
            self._pause_started = None
            self._paused_at = None
        # An explicit resume also acknowledges a pause withdrawn before the
        # worker reached a safe point. No command at startup emits no event.
        if self._ack_revision != self._revision:
            self.send({'type': 'control', 'state': 'running', 'revision': self._revision,
                       'paused_seconds': self.paused_seconds})
            self._ack_revision = self._revision
