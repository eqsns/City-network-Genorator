from pathlib import Path
from unittest.mock import Mock
import desktop_platform as dp
import bootstrap


def test_windows_keeps_portable_data_and_preferences(tmp_path):
    paths = dp.paths_for(tmp_path, platform='win32', environ={'LOCALAPPDATA': str(tmp_path / 'local')})
    assert paths.data == tmp_path / 'data'
    assert paths.preference == tmp_path / 'local' / 'SWMManywhereStudio' / 'data-location.txt'
    assert paths.renderer == 'edgechromium'


def test_mac_never_uses_bundle_for_writable_files(tmp_path):
    bundle = tmp_path / '移动 中文.app' / 'Contents' / 'MacOS'
    paths = dp.paths_for(bundle, platform='darwin', home=tmp_path / '用户')
    assert paths.data == tmp_path / '用户/Library/Application Support/SWMManywhereStudio/data'
    assert paths.renderer == 'cocoa'
    for path in (paths.data, paths.preference, paths.webview, paths.startup_log):
        assert not path.is_relative_to(bundle)
    paths.preference.parent.mkdir(parents=True)
    custom = tmp_path / '我的模型'
    custom.mkdir()
    paths.preference.write_text(str(custom), encoding='utf-8')
    assert dp.preferred_data(paths) == custom
    bundle.mkdir(parents=True)
    paths.preference.write_text(str(bundle), encoding='utf-8')
    assert dp.preferred_data(paths) == paths.data


def test_mac_dialog_arguments_cannot_be_applescript(monkeypatch):
    run = Mock()
    monkeypatch.setattr(dp.subprocess, 'run', run)
    text = '" & do shell script "oops"\n中文'
    dp.show_message(dp.APP_NAME, text, platform='darwin', error=True)
    args = run.call_args.args[0]
    assert args[-2:] == [dp.APP_NAME, text]
    assert text not in args[2]
    assert run.call_args.kwargs['shell'] is False


def test_mac_open_folder_uses_absolute_argv(tmp_path, monkeypatch):
    run = Mock()
    monkeypatch.setattr(dp.subprocess, 'run', run)
    folder = tmp_path / '中文 $(example)'
    dp.open_folder(folder, platform='darwin')
    assert run.call_args.args[0] == ['/usr/bin/open', str(folder.resolve())]


def test_mac_source_and_frozen_native_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'ASSETS', tmp_path)
    monkeypatch.setattr(bootstrap.sys, 'platform', 'darwin')
    monkeypatch.setattr(bootstrap, 'FROZEN', False)
    assert bootstrap.whitebox_archive() == tmp_path / 'native/macos-arm64/whitebox_tools.zip'
    monkeypatch.setattr(bootstrap, 'FROZEN', True)
    assert bootstrap.whitebox_archive() == tmp_path / 'native/whitebox_tools.zip'
