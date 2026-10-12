"""Interrupt-safe storage for complete OSM responses using OSMnx cache keys."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from osmnx import _http, settings


def _complete(result):
    # An empty elements list is a valid response for e.g. an area with no river.
    return isinstance(result, dict) and isinstance(result.get('elements'), list) and 'remark' not in result


def retrieve(key: str) -> dict | None:
    """Treat missing, interrupted, or incomplete responses as a cache miss.

    Keep invalid files untouched: a subsequent successful download can replace
    them atomically, and callers never need to delete a user's cache to retry.
    """
    if not settings.use_cache:
        return None
    try:
        result = json.loads(_http._resolve_cache_filepath(key).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return None
    return result if _complete(result) else None


def save(key: str, result: dict) -> bool:
    """Atomically save a complete response; cache failures remain nonfatal.

    The original logical URL and OSMnx's path resolver preserve existing cache
    compatibility. A forced cancellation can leave a .tmp file, but it cannot
    expose a partly written .json file or corrupt a previous complete response.
    """
    if not settings.use_cache or not _complete(result):
        return False
    temporary = None
    try:
        target = _http._resolve_cache_filepath(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix=target.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
