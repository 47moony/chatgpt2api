@echo off
setlocal

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
