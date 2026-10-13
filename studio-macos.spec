# Native Apple Silicon build only; run through packaging/build_macos.py.
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata, collect_submodules

root = Path(SPECPATH)
sys.path.insert(0, str(root / 'vendor' / 'swmmanywhere-src'))
datas = [(str(root / 'web'), 'web'),
         (str(root / 'assets'), 'assets'),
         (str(root / 'native' / 'macos-arm64' / 'whitebox_tools.zip'), 'native'),
         (str(root / 'vendor' / 'swmmanywhere-src'), 'vendor/swmmanywhere-src'),
         (str(root / '.cache' / 'macos-licenses'), 'licenses'),
         (str(root / 'README_MAC.md'), '.'), (str(root / 'THIRD-PARTY-NOTICES.md'), '.')]
binaries = []
hiddenimports = ['webview.platforms.cocoa', 'scipy.special._cdflib', 'rasterio.sample', 'rasterio.vrt']
hiddenimports += collect_submodules('rasterio') + collect_submodules('pyogrio')
for package in ['swmmanywhere', 'netcomp', 'pywbt', 'pyflwdir', 'swmm.toolkit']:
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
for package in ['swmmanywhere', 'pystac', 'pystac-client', 'osmnx', 'pywbt']:
    datas += copy_metadata(package)
for package in ['pyflwdir', 'pyswmm', 'rasterio', 'pyogrio']:
    datas += collect_data_files(package, include_py_files=True)
a = Analysis([str(root / 'desktop.py')], pathex=[str(root), str(root / 'vendor' / 'swmmanywhere-src')],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter', 'IPython', 'notebook',
                       'pytest', 'pythonnet', 'clr', 'webview.platforms.edgechromium', 'webview.platforms.winforms'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SWMManywhereStudio',
          debug=False, strip=False, upx=False, console=False, target_arch='arm64',
          codesign_identity=os.environ.get('CNG_CODESIGN_IDENTITY'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='CNG-macos-arm64')
app = BUNDLE(coll, name='CNG 城市排水管网生成.app', bundle_identifier='io.github.eqsns.cng',
             icon=str(root / 'assets' / 'app-icon.icns'),
             info_plist={'CFBundleDisplayName': 'CNG 城市排水管网生成',
                         'CFBundleShortVersionString': '0.1.1', 'CFBundleVersion': '2',
                         'NSHighResolutionCapable': True})
