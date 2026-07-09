param(
    [switch]$WaitForUser
)

$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $PSScriptRoot
$profileDir = if ($env:NANA_PROFILE_DIR) { $env:NANA_PROFILE_DIR } else { "D:\Documents\Tools\VibeCoding\nana-chrome-profile" }
$projectUrl = if ($env:NANA_PROJECT_URL) { $env:NANA_PROJECT_URL } else { "https://labs.google/fx/zh/tools/flow/project/aae032c7-27b7-4ee2-85b1-944b48351403" }
$chromeProxyServer = if ($env:NANA_CHROME_PROXY_SERVER) { $env:NANA_CHROME_PROXY_SERVER.Trim() } else { "" }

function Use-ChromeProxy {
    if (-not $chromeProxyServer) {
        return $false
    }
    $lower = $chromeProxyServer.ToLower()
    return ($lower -ne "0" -and $lower -ne "direct" -and $lower -ne "none")
}

function Find-Chrome {
    $paths = @()
    if ($env:CHROME_EXE) {
        $paths += $env:CHROME_EXE
    }
    $paths += @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    return $paths | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}

function Stop-NanaProfileChrome {
    $matches = Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$profileDir*" }
    if ($matches) {
        $matches | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    }
}

$chrome = Find-Chrome
if (-not $chrome) {
    Write-Host "Chrome executable not found. Set CHROME_EXE to chrome.exe path."
    exit 1
}

New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
Stop-NanaProfileChrome

$devToolsActivePort = Join-Path $profileDir "DevToolsActivePort"
Remove-Item -LiteralPath $devToolsActivePort -Force -ErrorAction SilentlyContinue

$args = @(
    "--user-data-dir=$profileDir",
    "--no-first-run",
    "--no-default-browser-check"
)

if (Use-ChromeProxy) {
    $args += "--proxy-server=$chromeProxyServer"
    $args += "--proxy-bypass-list=<-loopback>"
}

$args += $projectUrl

Start-Process -FilePath $chrome -ArgumentList $args
Write-Host "Opened Nano Banana login Chrome without CDP/remote debugging."
Write-Host "Profile: $profileDir"
Write-Host "URL: $projectUrl"
if (Use-ChromeProxy) {
    Write-Host "Chrome proxy: $chromeProxyServer"
}

if ($WaitForUser) {
    Write-Host ""
    Write-Host "Finish Google login in the opened Chrome window, confirm Flow opens, then close that Chrome window."
    Read-Host "Press Enter here after login is complete"
    Stop-NanaProfileChrome
    Remove-Item -LiteralPath $devToolsActivePort -Force -ErrorAction SilentlyContinue
}
