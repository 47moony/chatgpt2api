@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

if "%CHATGPT2API_HOST_PORT%"=="" set "CHATGPT2API_HOST_PORT=3000"
if "%IMAGE_GATEWAY_PORT%"=="" set "IMAGE_GATEWAY_PORT=3110"
if "%IMAGE_GATEWAY_UPSTREAM_URL%"=="" set "IMAGE_GATEWAY_UPSTREAM_URL=http://127.0.0.1:%CHATGPT2API_HOST_PORT%"
if "%IMAGE_GATEWAY_PUBLIC_BASE_URL%"=="" (
  for /f "tokens=*" %%i in ('powershell -NoProfile -Command "$ip = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.InterfaceAlias -notlike 'vEthernet*' -and $_.AddressState -eq 'Preferred' } | Sort-Object @{Expression={ if ($_.PrefixOrigin -eq 'Dhcp') { 0 } else { 1 } }}, InterfaceMetric | Select-Object -First 1 -ExpandProperty IPAddress; if (-not $ip) { $ip = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.AddressState -eq 'Preferred' } | Sort-Object InterfaceMetric | Select-Object -First 1 -ExpandProperty IPAddress }; $ip"') do set "IMAGE_GATEWAY_LAN_IP=%%i"
  if not "!IMAGE_GATEWAY_LAN_IP!"=="" (
    set "IMAGE_GATEWAY_PUBLIC_BASE_URL=http://!IMAGE_GATEWAY_LAN_IP!:%IMAGE_GATEWAY_PORT%"
  ) else (
    set "IMAGE_GATEWAY_PUBLIC_BASE_URL=http://127.0.0.1:%IMAGE_GATEWAY_PORT%"
  )
)

echo Image gateway upstream: %IMAGE_GATEWAY_UPSTREAM_URL%
echo Image gateway public URL: %IMAGE_GATEWAY_PUBLIC_BASE_URL%
echo Image gateway listen port: %IMAGE_GATEWAY_PORT%
echo.
echo Gateway API key file: %CD%\data\image_gateway.key
echo Use this key only for /generate and /edit, not the main chatgpt2api auth-key.
echo.

set "IMAGE_GATEWAY_EXISTING_PID="
for /f "tokens=*" %%p in ('powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %IMAGE_GATEWAY_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($c) { $c.OwningProcess }"') do set "IMAGE_GATEWAY_EXISTING_PID=%%p"
if not "%IMAGE_GATEWAY_EXISTING_PID%"=="" (
  echo Existing image gateway found on port %IMAGE_GATEWAY_PORT% ^(PID %IMAGE_GATEWAY_EXISTING_PID%^). Restarting...
  powershell -NoProfile -Command "Stop-Process -Id %IMAGE_GATEWAY_EXISTING_PID% -Force"
  powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(10); while ((Get-Date) -lt $deadline) { if (-not (Get-NetTCPConnection -LocalPort %IMAGE_GATEWAY_PORT% -State Listen -ErrorAction SilentlyContinue)) { exit 0 }; Start-Sleep -Milliseconds 250 }; exit 1"
  if errorlevel 1 (
    echo Failed to release port %IMAGE_GATEWAY_PORT%. Please close the existing process manually.
    pause
    exit /b 1
  )
  echo Existing gateway stopped.
  echo.
)

.\.venv\Scripts\python.exe -m uvicorn image_gateway:app --host 0.0.0.0 --port %IMAGE_GATEWAY_PORT% --access-log
