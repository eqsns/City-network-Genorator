"""Native desktop integration without importing a GUI or calculation engine."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping

APP_ID = 'SWMManywhereStudio'
APP_NAME = 'CNG 城市排水管网生成'
PROJECT_URL = 'https://github.com/ImperialCollegeLondon/SWMManywhere'


@dataclass(frozen=True)
class DesktopPaths:
    platform: str
    data: Path
    preference: Path
    webview: Path
    startup_log: Path
    renderer: str | None
    user_agent: str


def paths_for(root: Path, *, platform: str | None = None, home: Path | None = None,
              environ: Mapping[str, str] | None = None) -> DesktopPaths:
    """Keep Windows portable paths; never write beside a macOS executable."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    if platform == 'win32':
        support = Path(environ.get('LOCALAPPDATA', str(root))) / APP_ID
        data, startup_log = root / 'data', root / 'startup-error.log'
        renderer, agent = 'edgechromium', 'Windows; WebView2'
    else:
        home = Path.home() if home is None else home
        if platform == 'darwin':
            support = home / 'Library' / 'Application Support' / APP_ID
            renderer, agent = 'cocoa', 'Macintosh; Apple Silicon; WebKit'
        else:
            support = Path(environ.get('XDG_DATA_HOME', str(home / '.local' / 'share'))) / APP_ID
            renderer, agent = None, 'Unix'
        data, startup_log = support / 'data', support / 'startup-error.log'
    return DesktopPaths(platform, data, support / 'data-location.txt', support / 'webview',
                        startup_log, renderer, f'{APP_ID}/0.1 ({agent}; {PROJECT_URL})')


def data_location_allowed(path: Path, platform: str) -> bool:
    if platform != 'darwin':
        return True
    # A moved/translocated .app must continue to use the same writable workspace.
    parts = [part.casefold() for part in path.resolve().parts]
    return not any(part.endswith('.app') or part == 'apptranslocation' for part in parts)


def preferred_data(paths: DesktopPaths) -> Path:
    try:
        saved = paths.preference.read_text(encoding='utf-8').strip()
        if saved:
            candidate = Path(saved)
            if candidate.is_dir() and data_location_allowed(candidate, paths.platform):
                return candidate
    except (OSError, ValueError):
        pass
    return paths.data


def open_folder(path: Path, *, platform: str | None = None) -> None:
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        os.startfile(str(path))
    else:
        executable = '/usr/bin/open' if platform == 'darwin' else 'xdg-open'
        # An absolute argument cannot be mistaken for an option; no shell interpolation.
        subprocess.run([executable, str(path.resolve())], check=True, shell=False)


def show_message(title: str, message: str, *, error: bool = False,
                 platform: str | None = None) -> None:
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 16 if error else 64)
    elif platform == 'darwin':
        # Pass every user/path/error string as argv, never as AppleScript source.
        icon = 'stop' if error else 'note'
        script = ('on run argv\n'
                  'display dialog (item 2 of argv) with title (item 1 of argv) '
                  'buttons {"好"} default button "好" with icon ' + icon + '\nend run')
        subprocess.run(['/usr/bin/osascript', '-e', script, title, message],
                       check=True, shell=False, capture_output=True)
    else:
        from tkinter import Tk, messagebox
        window = Tk()
        window.withdraw()
        try:
            (messagebox.showerror if error else messagebox.showinfo)(title, message, parent=window)
        finally:
            window.destroy()


def report_startup_failure(paths: DesktopPaths, details: str) -> None:
    try:
        paths.startup_log.parent.mkdir(parents=True, exist_ok=True)
        paths.startup_log.write_text(details, encoding='utf-8')
        message = f'启动失败，请查看日志：\n{paths.startup_log}\n{details[-1200:]}'
    except OSError:
        message = f'启动失败，无法写入日志：\n{paths.startup_log}\n{details[-1200:]}'
    try:
        show_message(APP_NAME, message, error=True, platform=paths.platform)
    except Exception:
        # A missing/broken native UI must not mask the original startup exception.
        if sys.stderr is not None:
            print(message, file=sys.stderr)
