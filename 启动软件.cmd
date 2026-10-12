@echo off
cd /d "%~dp0"
if exist "dist\SWMManywhereStudio\SWMManywhereStudio.exe" (
  start "" "dist\SWMManywhereStudio\SWMManywhereStudio.exe"
  exit /b 0
)
if not exist ".venv-build\Scripts\pythonw.exe" (
  echo Desktop build not found. See README_APP.md.
  pause
  exit /b 1
)
start "" ".venv-build\Scripts\pythonw.exe" desktop.py
