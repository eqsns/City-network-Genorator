"""Derive Mac build constraints; the resulting freeze must be made on a Mac."""
from pathlib import Path

WINDOWS_ONLY = {'clr-loader', 'pythonnet', 'pywin32-ctypes', 'win32-setctime', 'pefile'}
COCOA = ['pyobjc-core', 'pyobjc-framework-Cocoa', 'pyobjc-framework-Quartz',
         'pyobjc-framework-WebKit', 'pyobjc-framework-Security']


def mac_requirements(text):
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name = line.split('==')[0].lower().replace('_', '-')
        if name not in WINDOWS_ONLY:
            lines.append(line)
    return '# Derived constraints, not a verified macOS lockfile.\n' + '\n'.join(lines + COCOA) + '\n'


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    print(mac_requirements((root / 'requirements-lock.txt').read_text()), end='')
