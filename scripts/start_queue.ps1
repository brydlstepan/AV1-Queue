<#
.SYNOPSIS
    Foreground launcher for AV1 Queue server (visible console, for debugging).
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = (Resolve-Path "$ScriptDir\..").Path
$PythonExe = "$RootDir\vs\python-env\Scripts\python.exe"

Set-Location $RootDir

# Port override: set $env:AV1QUEUE_PORT before launching (or edit runGUI.bat /
# a shortcut's "Target" to prefix `set AV1QUEUE_PORT=9000 && `). Falls back to
# 8765 on anything unset or out of range.
$Port = 8765
if ($env:AV1QUEUE_PORT) {
    $parsedPort = 0
    if ([int]::TryParse($env:AV1QUEUE_PORT, [ref]$parsedPort) -and $parsedPort -gt 0 -and $parsedPort -le 65535) {
        $Port = $parsedPort
    } else {
        Write-Host "[!] AV1QUEUE_PORT='$($env:AV1QUEUE_PORT)' is not a valid port (1-65535) — using $Port" -ForegroundColor Yellow
    }
}

# Bind-host override: set $env:AV1QUEUE_HOST to reach the server from outside
# this machine — a specific interface IP (e.g. your Tailscale 100.x.x.x
# address; comma-separate more than one) or 0.0.0.0 for every interface
# (LAN included). Loopback (127.0.0.1) is always bound too, on top of this,
# so binding just a Tailscale IP gives you Tailscale + localhost WITHOUT
# exposing the LAN — the actual bind happens in core/run_server.py (uvicorn's
# own --host only takes one address, which can't express that combination).
# Unset = loopback only, unchanged from before this existed.
#
# There is NO AUTHENTICATION unless you also set AV1QUEUE_USERNAME /
# AV1QUEUE_PASSWORD (HTTP Basic Auth, skipped for loopback requests). Widening
# the host without credentials means anything that can reach it has full
# control (queue, settings, stored API keys). Only do this on a network you
# trust — a Tailscale tailnet is a reasonable case.
$extraHosts = @()
if ($env:AV1QUEUE_HOST) {
    $extraHosts = $env:AV1QUEUE_HOST -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    $authSet = [bool]($env:AV1QUEUE_USERNAME -and $env:AV1QUEUE_PASSWORD)
    $warn = "[!] AV1QUEUE_HOST='$($env:AV1QUEUE_HOST)' — server will also accept connections from other machines."
    if (-not $authSet) {
        $warn += " NO LOGIN is configured (set AV1QUEUE_USERNAME / AV1QUEUE_PASSWORD to add one) — only do this on a network you trust."
    }
    Write-Host $warn -ForegroundColor Yellow
}

if (-not (Test-Path $PythonExe)) {
    Write-Host "[!] Virtual environment not found. Running setup first..." -ForegroundColor Yellow
    & "$ScriptDir\setup.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] Setup failed (exit code $LASTEXITCODE). Cannot start the server." -ForegroundColor Red
        Write-Host "Fix the setup errors, then re-run setup.bat or this script." -ForegroundColor Yellow
        exit $LASTEXITCODE
    }
    if (-not (Test-Path $PythonExe)) {
        Write-Host "[!] Setup finished but virtual environment is still missing: $PythonExe" -ForegroundColor Red
        exit 1
    }
}

# Set PATH to include bin and vs/plugins64
$env:PATH = "$RootDir\bin;$RootDir\vs\plugins64;" + $env:PATH
$env:PYTHONPATH = "$RootDir;$RootDir\vs;$RootDir\core;" + $env:PYTHONPATH
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   STARTING AV1 QUEUE STUDIO" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
# Loopback always works locally regardless of AV1QUEUE_HOST, so the local
# link stays 127.0.0.1 even when also bound to a Tailscale/LAN interface.
$studioUrl = "http://127.0.0.1:$Port"
$esc = [char]27
Write-Host "URL: " -NoNewline -ForegroundColor Green
Write-Host ($esc + "]8;;" + $studioUrl + $esc + "\" + $studioUrl + $esc + "]8;;" + $esc + "\") -ForegroundColor Cyan
Write-Host "Ctrl+click the link, or copy: $studioUrl" -ForegroundColor DarkGray
foreach ($h in $extraHosts) {
    Write-Host "Also bound to: http://$($h):$Port (reachable from other machines)" -ForegroundColor DarkGray
}
Write-Host "Press Ctrl+C in this terminal to stop the server.`n" -ForegroundColor DarkGray

try {
    & $PythonExe "$RootDir\core\run_server.py"
    $code = $LASTEXITCODE
} catch {
    Write-Host "[!] Failed to start server: $_" -ForegroundColor Red
    $code = 1
}

if ($code -ne 0) {
    Write-Host "`n[!] Server exited with code $code" -ForegroundColor Red
    Write-Host "Press Enter to close..." -ForegroundColor Yellow
    [void][System.Console]::ReadLine()
    exit $code
}
