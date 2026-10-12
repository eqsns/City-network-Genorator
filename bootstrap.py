"""Read-only code/resource paths for source and frozen builds.

ROOT is the executable directory in a frozen build, including Contents/MacOS
inside a macOS .app. User data paths belong to desktop_platform, not ROOT.
"""
from pathlib import Path
import sys

FROZEN = getattr(sys, 'frozen', False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
ASSETS = Path(getattr(sys, '_MEIPASS', ROOT))
SOURCE = ASSETS / 'vendor' / 'swmmanywhere-src'
if SOURCE.exists():
    sys.path.insert(0, str(SOURCE))


def worker_command(*arguments):
    if FROZEN:
        return [sys.executable, '--worker', *map(str, arguments)]
    return [sys.executable, '-u', str(ROOT / 'desktop.py'), '--worker', *map(str, arguments)]


def whitebox_archive():
    native = ASSETS / 'native'
    if sys.platform == 'darwin' and not FROZEN:
        native = native / 'macos-arm64'
    return native / 'whitebox_tools.zip'
