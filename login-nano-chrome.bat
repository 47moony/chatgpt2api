@echo off
setlocal

cd /d "%~dp0"

if "%NANA_PROFILE_DIR%"=="" set "NANA_PROFILE_DIR=D:\Documents\Tools\VibeCoding\nana-chrome-profile"
if "%NANA_PROJECT_URL%"=="" set "NANA_PROJECT_URL=https://labs.google/fx/zh/tools/flow/project/aae032c7-27b7-4ee2-85b1-944b48351403"
if "%NANA_CHROME_PROXY_SERVER%"=="" set "NANA_CHROME_PROXY_SERVER=http://127.0.0.1:7897"

echo Nano Banana login repair profile: %NANA_PROFILE_DIR%
echo Nano Banana Flow URL: %NANA_PROJECT_URL%
echo Nano Banana Chrome proxy: %NANA_CHROME_PROXY_SERVER%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\login_nana_chrome.ps1" -WaitForUser
if errorlevel 1 (
  echo.
  echo Failed to open Nano Banana login Chrome.
  pause
  exit /b 1
)

echo.
echo Login repair finished. Start or restart start-image-gateway.bat to use Nano Banana.
