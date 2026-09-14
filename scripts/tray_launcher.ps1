<#
.SYNOPSIS
    Runs AV1 Queue Studio as a background process with a system tray icon —
    no console window, not shown on the taskbar. Right-click the tray icon to
    open the studio, start/pause/resume the queue, view the log, or stop the server.
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
$StudioUrl = "http://127.0.0.1:8765"
$StudioPort = 8765

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RootDir

if (-not (Test-Path $PythonExe)) {
    [System.Windows.Forms.MessageBox]::Show(
        "Virtual environment not found.`n`nDouble-click setup.bat in the project folder first.",
        "AV1 Queue Studio", "OK", "Error"
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

# Serialize start/attach so two quick runGUI.bat clicks cannot spawn two uvicorns.
$startMutex = New-Object System.Threading.Mutex($false, "Local\AV1QueueStudioServerStart")
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
                -ArgumentList "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", "8765", "--log-level", "info" `
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
                    "AV1 Queue Studio", "OK", "Error"
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
$trayIcon.Text = "AV1 Queue Studio"
$trayIcon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$openItem = $menu.Items.Add("Open Studio")
$startItem = $menu.Items.Add("Start Queue")
$pauseItem = $menu.Items.Add("Pause Queue")
$logItem = $menu.Items.Add("View Log")
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
            "AV1 Queue Studio", "OK", "Warning"
        ) | Out-Null
        return
    }
    if (-not (Invoke-QueueAction "start")) {
        [System.Windows.Forms.MessageBox]::Show(
            "Could not start the queue. Is the server running?",
            "AV1 Queue Studio", "OK", "Warning"
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
                "AV1 Queue Studio", "OK", "Warning"
            ) | Out-Null
        }
    } else {
        if (-not (Invoke-QueueAction "pause")) {
            [System.Windows.Forms.MessageBox]::Show(
                "Could not pause the queue. Is the server running?",
                "AV1 Queue Studio", "OK", "Warning"
            ) | Out-Null
        }
    }
})
$logItem.add_Click({ Start-Process notepad.exe $LogFile })

$stopItem.add_Click({
    if (-not $serverProc -or $serverProc.HasExited) {
        $serverProc = Resolve-ServerProcess
    }
    Stop-ServerTree $serverProc
    Remove-Item -Path $PidFile -ErrorAction SilentlyContinue
    $trayIcon.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})

$trayIcon.ShowBalloonTip(3000, "AV1 Queue Studio", "Running in the tray. Right-click for options.", [System.Windows.Forms.ToolTipIcon]::Info)

if ($freshStart -and (Test-ServerHealthy)) {
    Open-Studio
}

# Keep the process alive (no visible window) until Stop is chosen from the tray.
[System.Windows.Forms.Application]::Run()
