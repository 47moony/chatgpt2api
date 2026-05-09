@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "CUSTOM_BRANCH=custom/sub2api-export"
set "MAIN_BRANCH=main"
set "HAS_CHANGES="
set "STASHED="

echo [1/7] Checking git repository...
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo This directory is not a git repository.
  pause
  exit /b 1
)

echo [2/7] Checking remotes...
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

echo [3/7] Switching to %CUSTOM_BRANCH%...
git switch %CUSTOM_BRANCH%
if errorlevel 1 goto :fail

for /f "delims=" %%i in ('git status --porcelain') do set "HAS_CHANGES=1"
if defined HAS_CHANGES (
  set "STASH_NAME=auto-stash-before-update-%date:/=-%-%time::=-%"
  set "STASH_NAME=!STASH_NAME: =0!"
  echo [4/7] Stashing local uncommitted changes...
  git stash push -u -m "!STASH_NAME!"
  if errorlevel 1 goto :fail
  set "STASHED=1"
) else (
  echo [4/7] Working tree is clean.
)

echo [5/7] Fetching official upstream...
git fetch upstream
if errorlevel 1 goto :fail

echo [6/7] Merging upstream/%MAIN_BRANCH% into %CUSTOM_BRANCH%...
git merge upstream/%MAIN_BRANCH%
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
  echo Re-applying stashed local changes...
  git stash pop
  if errorlevel 1 (
    echo Stash pop had conflicts. Resolve them manually before building.
    pause
    exit /b 1
  )
)

echo [7/7] Rebuilding Docker containers from local source...
docker compose up -d --build
if errorlevel 1 goto :fail

echo.
echo Update complete. Current branch should be %CUSTOM_BRANCH%.
pause
exit /b 0

:fail
echo.
echo Update failed. Returning to %CUSTOM_BRANCH% if possible...
git switch %CUSTOM_BRANCH% >nul 2>nul
echo Check the message above. If a stash was created, inspect it with: git stash list
pause
exit /b 1
