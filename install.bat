@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m venv .venv
goto created
:use_python
python -m venv .venv
:created
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import tkinter, PIL.ImageGrab, PIL.ImageTk, numpy, cv2; print('Dependencies OK')"
if errorlevel 1 goto failed
echo Setup finished. Run run.bat.
pause
exit /b 0
:failed
echo Setup failed. Install Python 3.11 or newer from python.org with Tkinter and pip.
pause
exit /b 1
