@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo [1/3] Checking git repository...
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo This directory is not a git repository.
  pause
  exit /b 1
)

echo [2/3] Showing current branch and local changes...
git status --short --branch

echo [3/3] Rebuilding Docker containers from current local source...
if not defined NPM_CONFIG_PROXY (
  powershell -NoProfile -Command "if (Test-NetConnection -ComputerName 127.0.0.1 -Port 7897 -InformationLevel Quiet) { exit 0 } else { exit 1 }" >nul 2>nul
  if not errorlevel 1 (
    set "NPM_CONFIG_PROXY=http://host.docker.internal:7897"
  )
)

if defined NPM_CONFIG_PROXY (
  if not defined NPM_CONFIG_HTTPS_PROXY set "NPM_CONFIG_HTTPS_PROXY=!NPM_CONFIG_PROXY!"
  if not defined HTTP_PROXY set "HTTP_PROXY=!NPM_CONFIG_PROXY!"
  if not defined HTTPS_PROXY set "HTTPS_PROXY=!NPM_CONFIG_HTTPS_PROXY!"
  echo Using Docker build proxy: !NPM_CONFIG_PROXY!
)

docker compose up -d --build
if errorlevel 1 (
  echo.
  echo Build failed. Check the message above.
  pause
  exit /b 1
)

echo.
echo Build complete.
pause
exit /b 0
