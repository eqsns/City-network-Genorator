"""Small, atomic JSON snapshots with bounded retries for Windows file locks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time


def atomic_write_json(path, data):
    path = Path(path)
    # Serialization errors must leave the existing snapshot untouched.
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        delays = (.02, .04, .08, .16, .32)
        for attempt in range(len(delays) + 1):
            try:
                os.replace(temporary, path)
                return
            except OSError as exc:
                transient = isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in {5, 32, 33}
                if not transient or attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
