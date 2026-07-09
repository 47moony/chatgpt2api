$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $baseDir "data"
$cdpUrlFile = if ($env:NANA_CDP_URL_FILE) { $env:NANA_CDP_URL_FILE } else { Join-Path $dataDir "nana_cdp_url.txt" }
$portText = if ($env:NANA_CDP_PORT) { $env:NANA_CDP_PORT } else { "0" }
$port = [int]$portText
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

function Test-CdpUrl {
    param([string]$Url)
    if (-not $Url) {
        return $false
    }
    try {
        Invoke-RestMethod -Uri "$Url/json/version" -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Write-CdpUrl {
    param([string]$Url)
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($cdpUrlFile, $Url, $utf8NoBom)
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

function Test-NanaProfileChromeProxyMatches {
    if (-not (Use-ChromeProxy)) {
        return $true
    }
    $main = Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$profileDir*" -and $_.CommandLine -like "*--remote-debugging-port*" } |
        Select-Object -First 1
    if (-not $main) {
        return $true
    }
    $needle = "--proxy-server=$chromeProxyServer"
    return ($main.CommandLine -like "*$needle*")
}

function Get-PythonExe {
    $venvPython = Join-Path $baseDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }
    return "python"
}

function Invoke-NanaProbe {
    param([string]$Url)
    $probeScript = Join-Path $PSScriptRoot "nana_probe.py"
    if (-not (Test-Path -LiteralPath $probeScript)) {
        return $null
    }
    $python = Get-PythonExe
    try {
        $raw = (& $python $probeScript --cdp-url $Url --project-url $projectUrl --no-screenshot --click-landing-cta 2>&1) -join "`n"
        $jsonStart = $raw.IndexOf("{")
        if ($jsonStart -lt 0) {
            return $null
        }
        return $raw.Substring($jsonStart) | ConvertFrom-Json
    } catch {
        Write-Host "Nano Banana Flow probe failed: $($_.Exception.Message)"
        return $null
    }
}

function Test-NanaLoginRepairNeeded {
    param($Probe)
    if (-not $Probe) {
        return $false
    }
    $text = (@(
        $Probe.error,
        $Probe.flow_state,
        $Probe.reason,
        $Probe.body_text_head
    ) -join " ")
    if ($Probe.tabs) {
        foreach ($tab in $Probe.tabs) {
            $text += " "
            $text += (@($tab.url, $tab.title, $tab.text, $tab.body_text_head) -join " ")
        }
    }
    $lower = $text.ToLower()
    if ($Probe.needs_login) { return $true }
    if ($Probe.flow_state -eq "login_required") { return $true }
    if ($Probe.error -eq "google_login_required") { return $true }
    if ($Probe.error -eq "flow_project_tab_not_found" -and $lower -like "*accounts.google*") { return $true }
    if ($lower -like "*accounts.google*") { return $true }
    if ($lower -like "*signin/rejected*") { return $true }
    if ($lower -like "*browser or app may not be secure*") { return $true }
    if ($lower -like "*could not sign you in*") { return $true }
    if ($text -like "*无法登录*") { return $true }
    if ($text -like "*不安全*") { return $true }
    return $false
}

function Invoke-NanaLoginRepair {
    $loginScript = Join-Path $PSScriptRoot "login_nana_chrome.ps1"
    if (-not (Test-Path -LiteralPath $loginScript)) {
        Write-Host "Nano Banana login helper not found: $loginScript"
        return $false
    }
    Write-Host ""
    Write-Host "Google login is required or blocked in CDP Chrome."
    Write-Host "Opening the same Nano Banana profile without CDP so you can complete login manually."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $loginScript -WaitForUser
    return ($LASTEXITCODE -eq 0)
}

function Confirm-CdpReady {
    param([string]$Url)
    Write-CdpUrl $Url
    $probe = Invoke-NanaProbe $Url
    if ($probe -and $probe.ok) {
        Write-Host "Nano Banana Flow workspace verified."
        return $true
    }
    if (Test-NanaLoginRepairNeeded $probe) {
        if ($env:NANA_AUTO_LOGIN_REPAIR -eq "0") {
            Write-Host "Nano Banana login repair is disabled by NANA_AUTO_LOGIN_REPAIR=0."
            return $false
        }
        if ($env:NANA_LOGIN_REPAIR_ATTEMPTED -eq "1") {
            Write-Host "Nano Banana still requires login after one repair attempt."
            return $false
        }
        $env:NANA_LOGIN_REPAIR_ATTEMPTED = "1"
        if (-not (Invoke-NanaLoginRepair)) {
            return $false
        }
        & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath
        exit $LASTEXITCODE
    }
    if ($probe) {
        Write-Host "Nano Banana Flow probe is not fully ready yet: state=$($probe.flow_state), error=$($probe.error)"
    }
    return $true
}

if (-not (Test-NanaProfileChromeProxyMatches)) {
    Write-Host "Existing Nano Banana Chrome does not use NANA_CHROME_PROXY_SERVER=$chromeProxyServer. Restarting Chrome profile..."
    Stop-NanaProfileChrome
    Remove-Item -LiteralPath $cdpUrlFile -Force -ErrorAction SilentlyContinue
}

if ($env:NANA_CDP_URL -and (Test-CdpUrl $env:NANA_CDP_URL)) {
    if (Confirm-CdpReady $env:NANA_CDP_URL) {
        Write-Host "Chrome CDP already available at $env:NANA_CDP_URL."
        exit 0
    }
    exit 1
}

if (Test-Path -LiteralPath $cdpUrlFile) {
    $existingUrl = (Get-Content -Raw -LiteralPath $cdpUrlFile).Trim()
    if (Test-CdpUrl $existingUrl) {
        if (Confirm-CdpReady $existingUrl) {
            Write-Host "Chrome CDP already available at $existingUrl."
            exit 0
        }
        exit 1
    }
}

if ($port -gt 0) {
    $existing = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($existing) {
        $url = "http://127.0.0.1:$port"
        if (Test-CdpUrl $url) {
            if (Confirm-CdpReady $url) {
                Write-Host "Chrome CDP already listening on port $port (PID $($existing.OwningProcess))."
                exit 0
            }
            exit 1
        }
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
    "--remote-debugging-port=$port",
    "--remote-debugging-address=127.0.0.1",
    "--remote-allow-origins=*",
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
Write-Host "Started Chrome: $chrome"
if (Use-ChromeProxy) {
    Write-Host "Chrome proxy: $chromeProxyServer"
}

$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    if ($port -gt 0) {
        $url = "http://127.0.0.1:$port"
        if (Test-CdpUrl $url) {
            if (Confirm-CdpReady $url) {
                Write-Host "Chrome CDP available at $url."
                exit 0
            }
            exit 1
        }
    } elseif (Test-Path -LiteralPath $devToolsActivePort) {
        $lines = Get-Content -LiteralPath $devToolsActivePort -ErrorAction SilentlyContinue
        if ($lines -and $lines.Count -ge 1) {
            $dynamicPort = [int]$lines[0]
            $url = "http://127.0.0.1:$dynamicPort"
            if (Test-CdpUrl $url) {
                if (Confirm-CdpReady $url) {
                    Write-Host "Chrome CDP available at $url."
                    exit 0
                }
                exit 1
            }
        }
    }
    Start-Sleep -Milliseconds 500
}

Write-Host "Chrome started but CDP did not become available."
exit 1
