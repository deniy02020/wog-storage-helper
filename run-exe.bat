@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\WoGStorageHelper\WoGStorageHelper.exe" (
  echo Build the application with build.bat first.
  exit /b 1
)
set "WOG_HELPER_PROFILE=%~dp0profile"
start "" "dist\WoGStorageHelper\WoGStorageHelper.exe"
exit /b 0
