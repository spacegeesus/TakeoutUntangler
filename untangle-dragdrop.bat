@echo off
title Takeout Untangler
if "%~1"=="" (
  echo Drag your Google Takeout zip file ^(or the folder holding your takeout zips^) onto this .bat file.
  echo.
  pause
  exit /b 1
)
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" on the first screen, then try again.
  echo.
  pause
  exit /b 1
)
if "%UNTANGLE_OUT%"=="" set "UNTANGLE_OUT=%USERPROFILE%\Pictures\From Google Photos"
set "EXTRA="
if "%UNTANGLE_DRY%"=="1" set "EXTRA=--dry-run"
echo Output folder: %UNTANGLE_OUT%
echo.
python -m pip install -q piexif >nul 2>nul
python -I "%~dp0untangle.py" %* --out "%UNTANGLE_OUT%" %EXTRA%
echo.
echo Finished. Your library is in: %UNTANGLE_OUT%
pause
