@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing project Python environment. No dependencies were installed.
  exit /b 1
)
".venv\Scripts\python.exe" -B -m installer.build %*
exit /b %errorlevel%
