@echo off
setlocal

cd /d "%~dp0"

if "%~1"=="" (
  echo Usage:
  echo   init-fork.bat ^<your-fork-git-url^>
  echo Example:
  echo   init-fork.bat https://github.com/yourname/chatgpt2api.git
  pause
  exit /b 1
)

set "FORK_URL=%~1"
set "CUSTOM_BRANCH=custom/sub2api-export"

echo [1/6] Checking git repository...
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo This directory is not a git repository.
  pause
  exit /b 1
)

echo [2/6] Configuring remotes...
git remote get-url upstream >nul 2>nul
if errorlevel 1 (
  git remote get-url origin >nul 2>nul
  if errorlevel 1 (
    echo Missing current origin remote. Cannot infer official upstream.
    pause
    exit /b 1
  )
  git remote rename origin upstream
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git remote add origin "%FORK_URL%"
) else (
  git remote set-url origin "%FORK_URL%"
)

echo [3/6] Creating or switching custom branch...
git switch %CUSTOM_BRANCH% 2>nul
if errorlevel 1 git switch -c %CUSTOM_BRANCH%
if errorlevel 1 goto :fail

echo [4/6] Showing current changes. Review before committing:
git status --short

echo.
echo [5/6] Next step: commit your custom changes manually.
echo Recommended command, excluding config.json:
echo   git add api/accounts.py api/register.py services/account_service.py services/backup_service.py services/register/openai_register.py services/register_service.py services/sub2api_export_service.py web/src/app/accounts/page.tsx web/src/app/register/components/register-card.tsx web/src/lib/api.ts update-custom.bat init-fork.bat
echo   git commit -m "add sub2api export support"
echo.
echo [6/6] Then push:
echo   git push -u origin %CUSTOM_BRANCH%
echo.
echo Initialization finished.
pause
exit /b 0

:fail
echo Initialization failed. Check the message above.
pause
exit /b 1
