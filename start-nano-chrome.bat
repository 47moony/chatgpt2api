@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

if "%NANA_CDP_PORT%"=="" set "NANA_CDP_PORT=0"
if "%NANA_PROFILE_DIR%"=="" set "NANA_PROFILE_DIR=D:\Documents\Tools\VibeCoding\nana-chrome-profile"
if "%NANA_PROJECT_URL%"=="" set "NANA_PROJECT_URL=https://labs.google/fx/zh/tools/flow/project/aae032c7-27b7-4ee2-85b1-944b48351403"

echo Nano Banana Chrome CDP port: %NANA_CDP_PORT% (0 means auto)
echo Nano Banana Chrome profile: %NANA_PROFILE_DIR%
echo Nano Banana Flow URL: %NANA_PROJECT_URL%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_nana_chrome.ps1"

if errorlevel 1 (
  echo.
  echo Failed to start Nano Banana Chrome.
  pause
  exit /b 1
)

echo.
echo Chrome is starting. If this is the first run, log in to Google and keep the Flow project tab open.
echo If Google says the browser is unsafe, close this window and run login-nano-chrome.bat.
echo Then run:
echo   .\.venv\Scripts\python.exe scripts\nana_probe.py
echo.
