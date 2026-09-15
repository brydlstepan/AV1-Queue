<#
.SYNOPSIS
    Foreground launcher for AV1 Queue server (visible console, for debugging).
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = (Resolve-Path "$ScriptDir\..").Path
$PythonExe = "$RootDir\vs\python-env\Scripts\python.exe"

Set-Location $RootDir

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
$studioUrl = "http://127.0.0.1:8765"
$esc = [char]27
Write-Host "URL: " -NoNewline -ForegroundColor Green
Write-Host ($esc + "]8;;" + $studioUrl + $esc + "\" + $studioUrl + $esc + "]8;;" + $esc + "\") -ForegroundColor Cyan
Write-Host "Ctrl+click the link, or copy: $studioUrl" -ForegroundColor DarkGray
Write-Host "Press Ctrl+C in this terminal to stop the server.`n" -ForegroundColor DarkGray

try {
    & $PythonExe -m uvicorn server.app:app --host 127.0.0.1 --port 8765 --log-level info
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
