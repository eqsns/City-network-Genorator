"""Own each worker process tree so cancellation also stops native tools."""
import os
import signal
import subprocess

IS_WINDOWS = os.name == 'nt'


def start_worker(command, **kwargs):
    if IS_WINDOWS:
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs['start_new_session'] = True
    proc = subprocess.Popen(command, **kwargs)
    proc._cng_owns_session = not IS_WINDOWS
    return proc


def terminate_tree(proc):
    if proc is None:
        return
    if getattr(proc, '_cng_owns_session', False):
        # Only workers created by start_worker own this process group.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The leader may exit before a native child; reap the whole group.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=10)
    elif proc.poll() is None:
        if IS_WINDOWS:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
