# PyInstaller folder build: isolated GUI/worker executable and bundled GIS runtimes.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata, collect_submodules

root=Path(SPECPATH)
sys.path.insert(0,str(root/'vendor'/'swmmanywhere-src'))
datas=[(str(root/'web'),'web'),
       (str(root/'native'/'whitebox_tools.zip'),'native'),
       (str(root/'native'/'WebView2'),'native/WebView2'),
       (str(root/'vendor'/'swmmanywhere-src'),'vendor/swmmanywhere-src'),
       (str(root/'licenses'),'licenses')]
binaries=[]
hiddenimports=['webview.platforms.edgechromium','webview.platforms.winforms',
               'scipy.special._cdflib','rasterio.sample','rasterio.vrt']
hiddenimports+=collect_submodules('rasterio')
hiddenimports+=collect_submodules('pyogrio')
for package in ['swmmanywhere','netcomp','pywbt','pyflwdir','swmm.toolkit','pythonnet','clr_loader']:
    d,b,h=collect_all(package)
    datas+=d;binaries+=b;hiddenimports+=h
for package in ['swmmanywhere','pystac','pystac-client','osmnx','pywbt']:
    datas+=copy_metadata(package)
datas+=collect_data_files('pyflwdir',include_py_files=True)
datas+=collect_data_files('pyswmm',include_py_files=True)
datas+=collect_data_files('rasterio')
datas+=collect_data_files('pyogrio')
a=Analysis([str(root/'desktop.py')],pathex=[str(root),str(root/'vendor'/'swmmanywhere-src')],
           binaries=binaries,datas=datas,hiddenimports=hiddenimports,
           excludes=['PyQt5','PyQt6','PySide2','PySide6','tkinter','IPython','notebook','pytest'],
           noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='SWMManywhereStudio',debug=False,
        bootloader_ignore_signals=False,strip=False,upx=False,console=False,disable_windowed_traceback=False)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='SWMManywhereStudio')
