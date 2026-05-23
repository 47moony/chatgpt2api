@echo off
setlocal

if "%IMAGE_GATEWAY_PORT%"=="" set "IMAGE_GATEWAY_PORT=3110"

echo Image gateway listen port: %IMAGE_GATEWAY_PORT%
echo.

set "IMAGE_GATEWAY_EXISTING_PID="
for /f "tokens=*" %%p in ('powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %IMAGE_GATEWAY_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($c) { $c.OwningProcess }"') do set "IMAGE_GATEWAY_EXISTING_PID=%%p"

if "%IMAGE_GATEWAY_EXISTING_PID%"=="" (
  echo Image gateway is not running on port %IMAGE_GATEWAY_PORT%.
  echo.
  pause
  exit /b 0
)

echo Stopping image gateway PID %IMAGE_GATEWAY_EXISTING_PID%...
powershell -NoProfile -Command "Stop-Process -Id %IMAGE_GATEWAY_EXISTING_PID% -Force"
echo Done.
echo.
pause
