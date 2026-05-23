@echo off
setlocal

cd /d "%~dp0"

if "%IMAGE_GATEWAY_UPSTREAM_URL%"=="" set "IMAGE_GATEWAY_UPSTREAM_URL=http://127.0.0.1:3000"
if "%IMAGE_GATEWAY_PUBLIC_BASE_URL%"=="" (
  for /f "tokens=*" %%i in ('powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Sort-Object InterfaceMetric | Select-Object -First 1 -ExpandProperty IPAddress)"') do set "IMAGE_GATEWAY_LAN_IP=%%i"
  if not "%IMAGE_GATEWAY_LAN_IP%"=="" (
    set "IMAGE_GATEWAY_PUBLIC_BASE_URL=http://%IMAGE_GATEWAY_LAN_IP%:3010"
  ) else (
    set "IMAGE_GATEWAY_PUBLIC_BASE_URL=http://127.0.0.1:3010"
  )
)

echo Image gateway upstream: %IMAGE_GATEWAY_UPSTREAM_URL%
echo Image gateway public URL: %IMAGE_GATEWAY_PUBLIC_BASE_URL%
echo.
echo Gateway API key file: %CD%\data\image_gateway.key
echo Use this key only for /generate and /edit, not the main chatgpt2api auth-key.
echo.

.\.venv\Scripts\python.exe -m uvicorn image_gateway:app --host 0.0.0.0 --port 3010 --access-log
