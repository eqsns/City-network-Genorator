from unittest.mock import Mock
import subprocess
import signal
import os
import sys
import time
import pytest
import process_control as control


def test_worker_gets_private_posix_session(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(control, 'IS_WINDOWS', False)
    monkeypatch.setattr(control.subprocess, 'Popen', popen)
    proc = control.start_worker(['python', 'worker.py'], cwd='/tmp')
    assert popen.call_args.kwargs['start_new_session'] is True
    assert proc._cng_owns_session is True


def test_exit_of_leader_does_not_leave_native_children(monkeypatch):
    killpg = Mock()
    monkeypatch.setattr(control.os, 'killpg', killpg, raising=False)
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    proc = Mock(pid=1234, _cng_owns_session=True)
    proc.poll.return_value = 0
    control.terminate_tree(proc)
    assert [args.args for args in killpg.call_args_list] == [(1234, signal.SIGTERM), (1234, 9)]


def test_stubborn_group_gets_killed_after_timeout(monkeypatch):
    killpg = Mock()
    monkeypatch.setattr(control.os, 'killpg', killpg, raising=False)
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    proc = Mock(pid=1234, _cng_owns_session=True)
    proc.wait.side_effect = [subprocess.TimeoutExpired('worker', 10), 0]
    control.terminate_tree(proc)
    assert killpg.call_args.args == (1234, 9)


def test_unowned_process_never_signals_parent_group(monkeypatch):
    killpg = Mock()
    monkeypatch.setattr(control, 'IS_WINDOWS', False)
    monkeypatch.setattr(control.os, 'killpg', killpg, raising=False)
    proc = Mock(_cng_owns_session=False)
    proc.poll.return_value = None
    control.terminate_tree(proc)
    proc.terminate.assert_called_once()
    killpg.assert_not_called()


@pytest.mark.skipif(os.name == 'nt', reason='Requires POSIX process groups; run on Mac')
def test_real_posix_cancel_stops_native_child(tmp_path):
    marker = tmp_path / 'native-child-alive'
    child = tmp_path / 'child.py'
    child.write_text('import signal,time\nfrom pathlib import Path\n'
                     'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                     f'p=Path({str(marker)!r})\n'
                     'while True:\n p.write_text(str(time.monotonic()))\n time.sleep(.03)\n')
    parent = tmp_path / 'parent.py'
    parent.write_text('import subprocess,sys,time\n'
                      f'subprocess.Popen([sys.executable,{str(child)!r}])\n'
                      'time.sleep(90)\n')
    proc = control.start_worker([sys.executable, str(parent)])
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        assert marker.exists()
        control.terminate_tree(proc)
        time.sleep(.2)
        stopped = marker.read_text()
        time.sleep(.3)
        assert marker.read_text() == stopped
        assert proc.poll() is not None
    finally:
        control.terminate_tree(proc)
