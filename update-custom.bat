@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo [1/8] Checking git repository...
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo This directory is not a git repository.
  pause
  exit /b 1
)

set "CUSTOM_BRANCH=custom/sub2api-export"
set "MAIN_BRANCH=main"

echo [2/8] Checking remotes...
git remote get-url upstream >nul 2>nul
if errorlevel 1 (
  echo Missing upstream remote. Add official repo with:
  echo   git remote rename origin upstream
  echo   git remote add origin ^<your fork url^>
  pause
  exit /b 1
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo Missing origin remote. Add your fork with:
  echo   git remote add origin ^<your fork url^>
  pause
  exit /b 1
)

for /f "delims=" %%i in ('git status --porcelain') do set "HAS_CHANGES=1"
if defined HAS_CHANGES (
  set "STASH_NAME=auto-stash-before-update-%date:/=-%-%time::=-%"
  set "STASH_NAME=!STASH_NAME: =0!"
  echo [3/8] Stashing local uncommitted changes...
  git stash push -u -m "!STASH_NAME!"
  if errorlevel 1 (
    echo Failed to stash local changes.
    pause
    exit /b 1
  )
) else (
  echo [3/8] Working tree is clean.
)

echo [4/8] Fetching official upstream...
git fetch upstream
if errorlevel 1 goto :fail

echo [5/8] Updating local main from upstream/main...
git switch %MAIN_BRANCH%
if errorlevel 1 goto :fail
git merge --ff-only upstream/%MAIN_BRANCH%
if errorlevel 1 goto :fail

echo [6/8] Merging main into %CUSTOM_BRANCH%...
git switch %CUSTOM_BRANCH%
if errorlevel 1 goto :fail
git merge %MAIN_BRANCH%
if errorlevel 1 (
  echo Merge conflict detected. Resolve conflicts, then run:
  echo   git add ^<files^>
  echo   git commit
  echo After that, run Docker build manually:
  echo   docker compose up -d --build
  pause
  exit /b 1
)

if defined HAS_CHANGES (
  echo [7/8] Re-applying stashed local changes...
  git stash pop
  if errorlevel 1 (
    echo Stash pop had conflicts. Resolve them manually before building.
    pause
    exit /b 1
  )
) else (
  echo [7/8] No local stash to apply.
)

echo [8/8] Rebuilding Docker containers...
docker compose up -d --build
if errorlevel 1 goto :fail

echo.
echo Update complete.
pause
exit /b 0

:fail
echo.
echo Update failed. Check the message above.
pause
exit /b 1
