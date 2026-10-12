from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging'))
import source_archive


@pytest.mark.parametrize('name', ['data/runs/private.json', '.env', 'native/tool.exe', '../outside.py'])
def test_source_package_refuses_sensitive_or_generated_files(monkeypatch, name):
    monkeypatch.setattr(source_archive.subprocess, 'check_output', lambda *a, **kw: name.encode() + b'\0')
    with pytest.raises(ValueError):
        list(source_archive.source_files())
