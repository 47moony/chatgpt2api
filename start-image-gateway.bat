@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

if "%CHATGPT2API_HOST_PORT%"=="" (
  if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
      if /I "%%a"=="CHATGPT2API_HOST_PORT" set "CHATGPT2API_HOST_PORT=%%b"
    )
  )
)
if "%CHATGPT2API_HOST_PORT%"=="" set "CHATGPT2API_HOST_PORT=3300"
if "%IMAGE_GATEWAY_PORT%"=="" set "IMAGE_GATEWAY_PORT=3200"
if "%IMAGE_GATEWAY_MAX_CONCURRENT_REQUESTS%"=="" set "IMAGE_GATEWAY_MAX_CONCURRENT_REQUESTS=2"

if "%IMAGE_GATEWAY_LAN_IP%"=="" (
  for /f "tokens=*" %%i in ('powershell -NoProfile -Command "$ip = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.InterfaceAlias -notlike 'vEthernet*' -and $_.AddressState -eq 'Preferred' } | Sort-Object @{Expression={ if ($_.PrefixOrigin -eq 'Dhcp') { 0 } else { 1 } }}, InterfaceMetric | Select-Object -First 1 -ExpandProperty IPAddress; if (-not $ip) { $ip = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.AddressState -eq 'Preferred' } | Sort-Object InterfaceMetric | Select-Object -First 1 -ExpandProperty IPAddress }; $ip"') do set "IMAGE_GATEWAY_LAN_IP=%%i"
)
if "%IMAGE_GATEWAY_UPSTREAM_URL%"=="" (
  if not "%IMAGE_GATEWAY_LAN_IP%"=="" (
    set "IMAGE_GATEWAY_UPSTREAM_URL=http://%IMAGE_GATEWAY_LAN_IP%:%CHATGPT2API_HOST_PORT%"
  ) else (
    set "IMAGE_GATEWAY_UPSTREAM_URL=http://localhost:%CHATGPT2API_HOST_PORT%"
  )
)
if "%IMAGE_GATEWAY_PUBLIC_BASE_URL%"=="" (
  if not "!IMAGE_GATEWAY_LAN_IP!"=="" (
    set "IMAGE_GATEWAY_PUBLIC_BASE_URL=http://!IMAGE_GATEWAY_LAN_IP!:%IMAGE_GATEWAY_PORT%"
  ) else (
    set "IMAGE_GATEWAY_PUBLIC_BASE_URL=http://127.0.0.1:%IMAGE_GATEWAY_PORT%"
  )
)

echo Image gateway upstream: %IMAGE_GATEWAY_UPSTREAM_URL%
echo Image gateway public URL: %IMAGE_GATEWAY_PUBLIC_BASE_URL%
echo Image gateway listen port: %IMAGE_GATEWAY_PORT%
echo Image gateway max concurrent requests: %IMAGE_GATEWAY_MAX_CONCURRENT_REQUESTS%
echo.
echo Gateway API key file: %CD%\data\image_gateway.key
echo Use this key only for /generate and /edit, not the main chatgpt2api auth-key.
echo.

powershell -NoProfile -Command "$port = [int]$env:IMAGE_GATEWAY_PORT; $blocked = $false; $hit = ''; netsh interface ipv4 show excludedportrange protocol=tcp | ForEach-Object { if ($_ -match '^\s*(\d+)\s+(\d+)') { $start = [int]$matches[1]; $end = [int]$matches[2]; if ($port -ge $start -and $port -le $end) { $blocked = $true; $hit = \"$start-$end\" } } }; if ($blocked) { Write-Host \"Port $port is inside a Windows excluded TCP port range: $hit\"; exit 2 }; exit 0"
if errorlevel 2 (
  echo.
  echo Windows has reserved TCP port %IMAGE_GATEWAY_PORT%, so the gateway cannot bind to it.
  echo Choose a port outside the excluded range with:
  echo   set IMAGE_GATEWAY_PORT=3210
  echo   start-image-gateway.bat
  echo Or remove the Windows port exclusion as Administrator if you must keep %IMAGE_GATEWAY_PORT%.
  echo.
  pause
  exit /b 1
)

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
if errorlevel 1 (
  echo.
  echo Image gateway exited with error code %ERRORLEVEL%.
  pause
  exit /b %ERRORLEVEL%
)
