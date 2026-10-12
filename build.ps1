$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$buildPython = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
if (!(Test-Path $buildPython)) { throw 'Create .venv-build with Python 3.12 and install requirements-lock.txt first.' }
& $buildPython packaging\collect_licenses.py
if ($LASTEXITCODE -ne 0) { throw 'License collection failed' }
& $buildPython -m PyInstaller --noconfirm --distpath .cache\release-stage studio.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Copy-Item README_APP.md .cache\release-stage\SWMManywhereStudio\使用说明.md
Copy-Item LICENSE .cache\release-stage\SWMManywhereStudio\LICENSE
Copy-Item requirements-lock.txt .cache\release-stage\SWMManywhereStudio\requirements-lock.txt
Copy-Item THIRD-PARTY-NOTICES.md .cache\release-stage\SWMManywhereStudio\THIRD-PARTY-NOTICES.md
Copy-Item VALIDATION.md .cache\release-stage\SWMManywhereStudio\验收记录.md
Copy-Item TEST_1000_20261008.md .cache\release-stage\SWMManywhereStudio\1000平方公里验收.md
Copy-Item TEST_STATE_20261009.md .cache\release-stage\SWMManywhereStudio\状态保存修复验收.md
Copy-Item TEST_GEOMETRY_20261009.md .cache\release-stage\SWMManywhereStudio\道路几何修复验收.md
Copy-Item TEST_COLD_600_20261009.md .cache\release-stage\SWMManywhereStudio\600平方公里无缓存验收.md
Copy-Item TEST_PAUSE_CACHE_20261010.md .cache\release-stage\SWMManywhereStudio\暂停恢复与缓存验收.md
Copy-Item TEST_EDGE_CASES_20261011.md .cache\release-stage\SWMManywhereStudio\边界情况补测.md
Copy-Item TEST_RELEASE_20261011.md .cache\release-stage\SWMManywhereStudio\发布包验收.md
New-Item -ItemType Directory -Force dist\SWMManywhereStudio | Out-Null
Copy-Item .cache\release-stage\SWMManywhereStudio\* dist\SWMManywhereStudio -Recurse -Force
