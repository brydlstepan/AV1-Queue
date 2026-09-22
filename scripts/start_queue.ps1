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
$studioUrl = "http://127.0.0.1:$Port"
$esc = [char]27
Write-Host "URL: " -NoNewline -ForegroundColor Green
Write-Host ($esc + "]8;;" + $studioUrl + $esc + "\" + $studioUrl + $esc + "]8;;" + $esc + "\") -ForegroundColor Cyan
Write-Host "Ctrl+click the link, or copy: $studioUrl" -ForegroundColor DarkGray
Write-Host "Press Ctrl+C in this terminal to stop the server.`n" -ForegroundColor DarkGray

try {
    & $PythonExe -m uvicorn server.app:app --host 127.0.0.1 --port $Port --log-level info
    $code = $LASTEXITCODE
} catch {
    Write-Host "[!] Failed to start server: $_" -ForegroundColor Red
    $code = 1
}

if ($code -ne 0) {
    Write-Host "`n[!] Uvicorn exited with code $code" -ForegroundColor Red
    Write-Host "Press Enter to close..." -ForegroundColor Yellow
    [void][System.Console]::ReadLine()
    exit $code
}
