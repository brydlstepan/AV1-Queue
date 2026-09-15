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

function Resolve-Python {
    # Prefer the Windows py launcher so we get a real 3.12+ even when
    # `python` is the Store stub or an older install.
    $candidates = @(
        @{ Cmd = "py"; Args = @("-3.12") },
        @{ Cmd = "py"; Args = @("-3") },
        @{ Cmd = "python"; Args = @() },
        @{ Cmd = "python3"; Args = @() }
    )

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
            if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 12)) { continue }

            return @{
                Exe     = $c.Cmd
                Args    = @($c.Args)
                Version = "$major.$minor.$patch"
            }
        } catch {
            continue
        }
    }
    return $null
}

Write-Banner "AV1 QUEUE STUDIO SETUP"

Write-Host "Repo root : $RootDir"
Write-Host "What this does:"
Write-Host "  1. Download missing FFmpeg, hdr10plus_tool, SVT-AV1 (if needed)"
Write-Host "  2. Create vs\python-env and install/update Python packages"
Write-Host "  3. Install missing VapourSynth plugins (ffms2, vszip, vship)"
Write-Host "  Existing binaries/plugins are left alone; delete them to force a re-download."
Write-Host ""

$python = Resolve-Python
if (-not $python) {
    Write-Host "[ERROR] Python 3.12+ was not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install from https://www.python.org/downloads/" -ForegroundColor Yellow
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
Write-Host "Studio URL: http://localhost:8765"
Write-Host ""
exit 0
