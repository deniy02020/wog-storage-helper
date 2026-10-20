@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto failed
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -c "import PyInstaller; assert PyInstaller.__version__ == '6.22.3', 'Use requirements-build-lock.txt'"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" ocr_resources.py --fetch-native
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --distpath "%~dp0dist" --workpath "%~dp0build" WoGStorageHelper.spec
if errorlevel 1 goto failed
if not exist "dist\WoGStorageHelper\WoGStorageHelper.exe" goto failed
echo Build ready: dist\WoGStorageHelper\WoGStorageHelper.exe
echo Distribute the whole WoGStorageHelper folder, including _internal.
exit /b 0
:missing
echo Missing project .venv. Prepare it with requirements-build-lock.txt before building.
exit /b 1
:failed
echo Build failed. No release has been published.
exit /b 1
