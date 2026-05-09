@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "CUSTOM_BRANCH=custom/sub2api-export"
set "MAIN_BRANCH=main"
set "START_BRANCH="
set "HAS_CHANGES="
set "STASHED="

for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set "START_BRANCH=%%b"

echo [1/9] Checking git repository...
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo This directory is not a git repository.
  pause
  exit /b 1
)

echo [2/9] Checking remotes...
git remote get-url upstream >nul 2>nul
if errorlevel 1 (
  echo Missing upstream remote. Expected official repo remote named upstream.
  pause
  exit /b 1
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo Missing origin remote. Expected your fork remote named origin.
  pause
  exit /b 1
)

if not "%START_BRANCH%"=="%CUSTOM_BRANCH%" (
  echo [3/9] Switching to %CUSTOM_BRANCH% before updating...
  git switch %CUSTOM_BRANCH%
  if errorlevel 1 goto :fail_return
) else (
  echo [3/9] Already on %CUSTOM_BRANCH%.
)

for /f "delims=" %%i in ('git status --porcelain') do set "HAS_CHANGES=1"
if defined HAS_CHANGES (
  set "STASH_NAME=auto-stash-before-update-%date:/=-%-%time::=-%"
  set "STASH_NAME=!STASH_NAME: =0!"
  echo [4/9] Stashing local uncommitted changes...
  git stash push -u -m "!STASH_NAME!"
  if errorlevel 1 goto :fail_return
  set "STASHED=1"
) else (
  echo [4/9] Working tree is clean.
)

echo [5/9] Fetching official upstream...
git fetch upstream
if errorlevel 1 goto :fail_return

echo [6/9] Updating local %MAIN_BRANCH% from upstream/%MAIN_BRANCH%...
git switch %MAIN_BRANCH%
if errorlevel 1 goto :fail_return
git merge --ff-only upstream/%MAIN_BRANCH%
if errorlevel 1 goto :fail_return

echo [7/9] Merging %MAIN_BRANCH% into %CUSTOM_BRANCH%...
git switch %CUSTOM_BRANCH%
if errorlevel 1 goto :fail_return
git merge %MAIN_BRANCH%
if errorlevel 1 (
  echo.
  echo Merge conflict detected on %CUSTOM_BRANCH%.
  echo Resolve conflicts, then run:
  echo   git add ^<files^>
  echo   git commit
  echo   docker compose up -d --build
  pause
  exit /b 1
)

if defined STASHED (
  echo [8/9] Re-applying stashed local changes...
  git stash pop
  if errorlevel 1 (
    echo Stash pop had conflicts. Resolve them manually before building.
    pause
    exit /b 1
  )
) else (
  echo [8/9] No local stash to apply.
)

echo [9/9] Rebuilding Docker containers from local source...
docker compose up -d --build
if errorlevel 1 goto :fail_return

echo.
echo Update complete. Current branch should be %CUSTOM_BRANCH%.
pause
exit /b 0

:fail_return
echo.
echo Update failed. Returning to %CUSTOM_BRANCH% if possible...
git switch %CUSTOM_BRANCH% >nul 2>nul
echo Check the message above. If a stash was created, inspect it with: git stash list
pause
exit /b 1
