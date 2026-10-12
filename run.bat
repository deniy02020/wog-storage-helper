@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" dashboard.py
if errorlevel 1 pause
exit /b
:missing
echo Run install.bat first.
pause
exit /b 1
