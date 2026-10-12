import importlib.util
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))
from macos_requirements import mac_requirements, WINDOWS_ONLY
import build_macos


def test_mac_constraints_keep_compute_versions_and_remove_windows_only():
    text = mac_requirements((ROOT / 'requirements-lock.txt').read_text())
    names = {line.split('==')[0].lower() for line in text.splitlines()}
    assert not names & WINDOWS_ONLY
    assert 'swmm-toolkit==0.17.0' in text
    assert 'pywebview==6.2.1' in text
    assert 'pyobjc-framework-WebKit' in text


def test_wrong_native_archive_rejected_before_extract_or_execute(tmp_path):
    native = tmp_path / 'whitebox_tools.zip'
    native.write_bytes(b'wrong platform or interrupted download')
    with pytest.raises(ValueError, match='checksum mismatch'):
        build_macos.verify_native(native)


def test_build_refuses_cross_compilation(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['build_macos.py'])
    monkeypatch.setattr(sys, 'platform', 'win32')
    with pytest.raises(SystemExit, match='cross-compilation'):
        build_macos.main()


def test_spec_does_not_bundle_windows_runtime():
    text = (ROOT / 'studio-macos.spec').read_text(encoding='utf-8')
    assert "target_arch='arm64'" in text
    assert "'macos-arm64' / 'whitebox_tools.zip'" in text
    assert 'WebView2' not in text
    compile(text, 'studio-macos.spec', 'exec')
