<#
.SYNOPSIS
    One-click setup and updater for AV1 Queue environment.
    Prefer launching via setup.bat in the repo root (double-click friendly).
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = (Resolve-Path "$ScriptDir\..").Path

function Write-Banner {
    param([string]$Text)
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host "   $Text" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host ""
}

# Supported Python minor versions: every requirement (and its deps) must ship
# a Windows wheel for it, or pip falls back to source builds that need MSVC.
# Keep in sync with SUPPORTED_PYTHON in setup_env.py and the README.
$MinPythonMinor = 12
$MaxPythonMinor = 15
$RecommendedPython = "3.13"

function Resolve-Python {
    # Prefer the Windows py launcher with explicit supported versions, so a
    # newer default (`py -3` = a release without wheels yet) or the Store
    # `python` stub doesn't win. Unsupported finds are returned too, so the
    # caller can name them in the error.
    $candidates = @(
        @{ Cmd = "py"; Args = @("-3.13") },
        @{ Cmd = "py"; Args = @("-3.12") },
        @{ Cmd = "py"; Args = @("-3.14") },
        @{ Cmd = "py"; Args = @("-3.15") },
        @{ Cmd = "py"; Args = @("-3") },
        @{ Cmd = "python"; Args = @() },
        @{ Cmd = "python3"; Args = @() }
    )
    $unsupported = @()

    # The Python install manager's py.exe can offer to download a missing
    # version instead of failing; probing should only look at what's installed.
    $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "false"

    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Cmd -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }

        try {
            # Use --version (not python -c) — PowerShell strips quotes from
            # native -c arguments and corrupts the snippet.
            $verArgs = @($c.Args) + @("--version")
            $output = & $c.Cmd @verArgs 2>&1
            if ($LASTEXITCODE -ne 0) { continue }

            $text = (@($output) | ForEach-Object { "$_" }) -join " "
            if ($text -notmatch 'Python\s+(\d+)\.(\d+)\.(\d+)') { continue }

            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            $patch = [int]$Matches[3]
            $found = @{
                Exe     = $c.Cmd
                Args    = @($c.Args)
                Version = "$major.$minor.$patch"
            }
            if ($major -ne 3 -or $minor -lt $MinPythonMinor -or $minor -gt $MaxPythonMinor) {
                $unsupported += $found
                continue
            }
            return @{ Python = $found; Unsupported = $unsupported }
        } catch {
            continue
        }
    }
    return @{ Python = $null; Unsupported = $unsupported }
}

Write-Banner "AV1 QUEUE STUDIO SETUP"

Write-Host "Repo root : $RootDir"
Write-Host "What this does:"
Write-Host "  1. Download missing HandBrakeCLI (SVT-AV1-Tritium build) and FFmpeg"
Write-Host "  2. Create vs\python-env and install/update Python packages"
Write-Host "  3. Install missing VapourSynth plugins (ffms2, vszip, vship)"
Write-Host "  Existing binaries/plugins are left alone; delete them to force a re-download."
Write-Host ""

$resolved = Resolve-Python
$python = $resolved.Python
if (-not $python) {
    $supported = "3.$MinPythonMinor-3.$MaxPythonMinor"
    $seen = @($resolved.Unsupported | ForEach-Object { $_.Version } | Select-Object -Unique)
    if ($seen.Count -gt 0) {
        Write-Host "[ERROR] Only unsupported Python found: $($seen -join ', ') (need $supported)." -ForegroundColor Red
        Write-Host "  Newer Pythons can lack prebuilt wheels for some packages;" -ForegroundColor Yellow
        Write-Host "  pip would then need Microsoft Visual C++ to build them." -ForegroundColor Yellow
    } else {
        Write-Host "[ERROR] Python $supported was not found." -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Install Python $RecommendedPython (it can sit next to other versions):" -ForegroundColor Yellow
    Write-Host "  https://www.python.org/downloads/  (Python install manager: py install $RecommendedPython)"
    Write-Host "  - Check 'Add python.exe to PATH'"
    Write-Host "  - Prefer the official installer (not only the Microsoft Store stub)"
    Write-Host "Then re-run setup.bat"
    exit 1
}

Write-Host "[OK] Using Python $($python.Version) ($($python.Exe) $($python.Args -join ' '))" -ForegroundColor Green
Write-Host ""

Set-Location $RootDir
$setupScript = Join-Path $ScriptDir "setup_env.py"
$invokeArgs = @($python.Args) + @($setupScript)

& $python.Exe @invokeArgs
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Host ""
    Write-Host "[ERROR] Setup failed with exit code $exitCode." -ForegroundColor Red
    Write-Host "Scroll up for the failing step. You can re-run setup.bat after fixing the issue." -ForegroundColor Yellow
    exit $exitCode
}

Write-Banner "SETUP COMPLETE"
Write-Host "Launch the studio:" -ForegroundColor Green
Write-Host "  Double-click  runGUI.bat" -ForegroundColor Yellow
Write-Host "  (or: .\scripts\start_queue.ps1 for a visible debug console)"
Write-Host ""
. "$PSScriptRoot\load_env.ps1"
$null = Import-DotEnv "$PSScriptRoot\..\.env"
$setupPort = if ($env:AV1QUEUE_PORT) { $env:AV1QUEUE_PORT } else { "8765" }
Write-Host "Studio URL: http://localhost:$setupPort"
Write-Host ""
exit 0
