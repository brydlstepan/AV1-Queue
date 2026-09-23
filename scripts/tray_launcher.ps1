<#
.SYNOPSIS
    Runs AV1 Queue as a background process with a system tray icon —
    no console window, not shown on the taskbar. Right-click the tray icon to
    open the studio, start/pause/resume the queue, view or live-tail the log,
    or stop the server.
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = (Resolve-Path "$ScriptDir\..").Path
$PythonExe = "$RootDir\vs\python-env\Scripts\python.exe"
$LogDir = "$RootDir\logs"
$LogFile = "$LogDir\server.log"
$ErrLogFile = "$LogDir\server.err.log"
$PidFile = "$LogDir\server.pid"

# Settings below can live in the project-root .env (see .env.example); the
# spawned server inherits them. Real environment variables take precedence.
. "$ScriptDir\load_env.ps1"
$envWarnings = Import-DotEnv "$RootDir\.env"
if ($envWarnings.Count -gt 0) {
    [System.Windows.Forms.MessageBox]::Show(
        ($envWarnings -join "`n"), "AV1 Queue", "OK", "Warning"
    ) | Out-Null
}

# Port override: set AV1QUEUE_PORT in .env, or in the environment before
# launching runGUI.bat (e.g. a shortcut's "Target" prefixed with
# `set AV1QUEUE_PORT=9000 && `). Falls back to 8765 on anything unset or out
# of range.
$StudioPort = 8765
if ($env:AV1QUEUE_PORT) {
    $parsedPort = 0
    if ([int]::TryParse($env:AV1QUEUE_PORT, [ref]$parsedPort) -and $parsedPort -gt 0 -and $parsedPort -le 65535) {
        $StudioPort = $parsedPort
    } else {
        [System.Windows.Forms.MessageBox]::Show(
            "AV1QUEUE_PORT='$($env:AV1QUEUE_PORT)' is not a valid port (1-65535). Using $StudioPort instead.",
            "AV1 Queue", "OK", "Warning"
        ) | Out-Null
    }
}
# Local tray control (health check, Open Studio, queue actions) always talks
# to loopback — it works regardless of bind host, and is simpler than trying
# to reach a wildcard/VPN address from the same machine.
$StudioUrl = "http://127.0.0.1:$StudioPort"

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
}
$authConfigured = [bool]($env:AV1QUEUE_USERNAME -and $env:AV1QUEUE_PASSWORD)

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RootDir

if (-not (Test-Path $PythonExe)) {
    [System.Windows.Forms.MessageBox]::Show(
        "Virtual environment not found.`n`nDouble-click setup.bat in the project folder first.",
        "AV1 Queue", "OK", "Error"
    ) | Out-Null
    exit 1
}

function Test-ServerHealthy {
    try {
        $r = Invoke-WebRequest -Uri "$StudioUrl/api/system" -TimeoutSec 1 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch { return $false }
}

function Get-ListenerPid([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn -and $conn.OwningProcess) {
            return [int]$conn.OwningProcess
        }
    } catch {}

    # Fallback when Get-NetTCPConnection is unavailable
    try {
        $lines = netstat -ano -p tcp 2>$null | Select-String ":$Port\s+.*LISTENING"
        foreach ($line in $lines) {
            if ($line -match '\s+(\d+)\s*$') {
                return [int]$Matches[1]
            }
        }
    } catch {}
    return $null
}

function Resolve-ServerProcess {
    if (Test-Path $PidFile) {
        $existingPid = Get-Content $PidFile -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($existingPid) {
            $proc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
            if ($proc) { return $proc }
        }
    }
    $listenPid = Get-ListenerPid $StudioPort
    if ($listenPid) {
        $proc = Get-Process -Id $listenPid -ErrorAction SilentlyContinue
        if ($proc) {
            $listenPid | Out-File -FilePath $PidFile -Encoding ascii
            return $proc
        }
    }
    return $null
}

function Stop-ServerTree([System.Diagnostics.Process]$Proc) {
    if (-not $Proc) { return }
    try {
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$($Proc.Id)" -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        if (-not $Proc.HasExited) {
            Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

function Get-QueueState {
    try {
        $r = Invoke-WebRequest -Uri "$StudioUrl/api/queue" -TimeoutSec 1 -UseBasicParsing
        return ($r.Content | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Invoke-QueueAction([string]$Action) {
    try {
        $body = (@{ action = $Action } | ConvertTo-Json -Compress)
        Invoke-WebRequest -Uri "$StudioUrl/api/queue/action" -Method POST `
            -ContentType "application/json; charset=utf-8" `
            -Body $body -TimeoutSec 3 -UseBasicParsing | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Open-Studio { Start-Process $StudioUrl }

function Open-LiveConsole {
    # A separate console window tailing the live server output — the
    # "View Log" item below opens a static snapshot in Notepad instead.
    if (-not (Test-Path $LogFile)) {
        New-Item -ItemType File -Path $LogFile -Force | Out-Null
    }
    $tailCmd = "`$Host.UI.RawUI.WindowTitle = 'AV1 Queue - Live Log'; " +
        "Write-Host 'Tailing $LogFile (Ctrl+C to close). Errors go to $ErrLogFile.' -ForegroundColor Cyan; " +
        "Get-Content -Path '$LogFile' -Wait -Tail 200"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-NoProfile", "-Command", $tailCmd
}

# Serialize start/attach so two quick runGUI.bat clicks cannot spawn two servers.
$startMutex = New-Object System.Threading.Mutex($false, "Local\AV1QueueServerStart_$StudioPort")
$mutexHeld = $false
try {
    $mutexHeld = $startMutex.WaitOne(30000)
} catch {
    $mutexHeld = $false
}

$serverProc = $null
$freshStart = $false

try {
    if (Test-ServerHealthy) {
        $serverProc = Resolve-ServerProcess
    } else {
        $env:PATH = "$RootDir\bin;$RootDir\vs\plugins64;" + $env:PATH
        $env:PYTHONPATH = "$RootDir;$RootDir\vs;$RootDir\core;" + $env:PYTHONPATH
        $env:PYTHONIOENCODING = "utf-8"
        $env:PYTHONUTF8 = "1"

        # Another launcher may have won the race while we waited on the mutex.
        if (Test-ServerHealthy) {
            $serverProc = Resolve-ServerProcess
        } else {
            $serverProc = Start-Process -FilePath $PythonExe `
                -ArgumentList "`"$RootDir\core\run_server.py`"" `
                -WorkingDirectory $RootDir `
                -WindowStyle Hidden `
                -RedirectStandardOutput $LogFile `
                -RedirectStandardError $ErrLogFile `
                -PassThru

            $serverProc.Id | Out-File -FilePath $PidFile -Encoding ascii
            $freshStart = $true

            $healthy = $false
            for ($i = 0; $i -lt 80; $i++) {
                if (Test-ServerHealthy) {
                    $healthy = $true
                    break
                }
                if ($serverProc.HasExited) { break }
                Start-Sleep -Milliseconds 250
            }

            if (-not $healthy) {
                Stop-ServerTree $serverProc
                Remove-Item -Path $PidFile -ErrorAction SilentlyContinue
                $detail = "Server did not become healthy on $StudioUrl."
                if (Test-Path $ErrLogFile) {
                    $detail += "`n`nCheck:`n$ErrLogFile"
                }
                [System.Windows.Forms.MessageBox]::Show(
                    $detail,
                    "AV1 Queue", "OK", "Error"
                ) | Out-Null
                exit 1
            }
        }
    }
} finally {
    if ($mutexHeld) {
        try { $startMutex.ReleaseMutex() } catch {}
    }
    try { $startMutex.Dispose() } catch {}
}

$icon = [System.Drawing.SystemIcons]::Application
$trayIcon = New-Object System.Windows.Forms.NotifyIcon
$trayIcon.Icon = $icon
$trayIcon.Text = if ($StudioPort -eq 8765) { "AV1 Queue" } else { "AV1 Queue ($StudioPort)" }
$trayIcon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$openItem = $menu.Items.Add("Open Studio")
$startItem = $menu.Items.Add("Start Queue")
$pauseItem = $menu.Items.Add("Pause Queue")
$logItem = $menu.Items.Add("View Log")
$consoleItem = $menu.Items.Add("Open Live Log Console")
$menu.Items.Add("-") | Out-Null
$stopItem = $menu.Items.Add("Stop Server && Exit")
$trayIcon.ContextMenuStrip = $menu

$menu.add_Opening({
    $state = Get-QueueState
    $jobCount = 0
    $isRunning = $false
    $isPaused = $false
    if ($state) {
        if ($state.jobs) { $jobCount = @($state.jobs).Count }
        $isRunning = [bool]$state.is_running
        $isPaused = [bool]$state.is_paused
    }

    $hasJobs = $jobCount -gt 0
    $startItem.Enabled = $hasJobs -and -not $isRunning
    if ($isPaused) {
        $pauseItem.Text = "Resume Queue"
        $pauseItem.Enabled = $hasJobs
    } else {
        $pauseItem.Text = "Pause Queue"
        $pauseItem.Enabled = $hasJobs -and $isRunning
    }
})

$openItem.add_Click({ Open-Studio })
$trayIcon.add_MouseDoubleClick({ param($s, $e) if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) { Open-Studio } })
$startItem.add_Click({
    if (-not (Test-ServerHealthy)) {
        [System.Windows.Forms.MessageBox]::Show(
            "Server is not responding. Check logs\server.err.log or re-run runGUI.bat.",
            "AV1 Queue", "OK", "Warning"
        ) | Out-Null
        return
    }
    if (-not (Invoke-QueueAction "start")) {
        [System.Windows.Forms.MessageBox]::Show(
            "Could not start the queue. Is the server running?",
            "AV1 Queue", "OK", "Warning"
        ) | Out-Null
    }
})
$pauseItem.add_Click({
    $state = Get-QueueState
    $isPaused = $false
    if ($state) { $isPaused = [bool]$state.is_paused }
    if ($isPaused) {
        if (-not (Invoke-QueueAction "resume")) {
            [System.Windows.Forms.MessageBox]::Show(
                "Could not resume the queue. Is the server running?",
                "AV1 Queue", "OK", "Warning"
            ) | Out-Null
        }
    } else {
        if (-not (Invoke-QueueAction "pause")) {
            [System.Windows.Forms.MessageBox]::Show(
                "Could not pause the queue. Is the server running?",
                "AV1 Queue", "OK", "Warning"
            ) | Out-Null
        }
    }
})
$logItem.add_Click({ Start-Process notepad.exe $LogFile })
$consoleItem.add_Click({ Open-LiveConsole })

$stopItem.add_Click({
    if (-not $serverProc -or $serverProc.HasExited) {
        $serverProc = Resolve-ServerProcess
    }
    Stop-ServerTree $serverProc
    Remove-Item -Path $PidFile -ErrorAction SilentlyContinue
    $trayIcon.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})

if ($extraHosts.Count -gt 0) {
    $hostList = $extraHosts -join ", "
    $balloonMsg = "Also bound to $($hostList):$StudioPort - reachable from other machines."
    $balloonMsg += if ($authConfigured) { " Login is required for non-local connections." } else { " NO LOGIN is configured; only run this on a network you trust." }
    $trayIcon.ShowBalloonTip(6000, "AV1 Queue", $balloonMsg, [System.Windows.Forms.ToolTipIcon]::Warning)
} else {
    $trayIcon.ShowBalloonTip(3000, "AV1 Queue", "Running in the tray. Right-click for options.", [System.Windows.Forms.ToolTipIcon]::Info)
}

if ($freshStart -and (Test-ServerHealthy)) {
    Open-Studio
}

# Keep the process alive (no visible window) until Stop is chosen from the tray.
[System.Windows.Forms.Application]::Run()
