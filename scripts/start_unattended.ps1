# ashare-system-v2 unattended startup script
# Starts WSL+OpenClaw + FastAPI + scheduler, no windows, auto-restarts on crash

param(
    [switch]$NoWatchdog,
    [switch]$NoScheduler,
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8100
)

$ProjectDir = "D:\Coding\lhjy\ashare-system-v2"
$WslProjectDir = "/mnt/d/Coding/lhjy/ashare-system-v2"
$Python = "$ProjectDir\.venv\Scripts\python.exe"
$LogDir = "$ProjectDir\logs"
$StateDir = "$ProjectDir\.ashare_state"
$WriteEndpointsScript = "$ProjectDir\scripts\write_service_endpoints.ps1"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Write-Log {
    param($Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $Message"
    Add-Content -Path "$LogDir\startup.log" -Value $line -Encoding UTF8
    Write-Host $line
}

function Stop-IfRunning {
    param(
        [int]$ProcessId,
        [string]$Label
    )
    if ($ProcessId -le 0) { return }
    try {
        Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        Write-Log "Stopped existing $Label PID=$ProcessId"
    } catch { }
}

function Test-HttpReady {
    param(
        [string]$Uri,
        [int]$TimeoutSec = 5,
        [string]$Property = "",
        [string]$ExpectedValue = ""
    )
    try {
        $resp = Invoke-RestMethod -Uri $Uri -TimeoutSec $TimeoutSec -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($Property)) {
            return $true
        }
        return "$($resp.$Property)" -eq $ExpectedValue
    } catch {
        return $false
    }
}

function Find-PythonProcessIds {
    param(
        [string]$CommandLinePattern
    )
    try {
        return @(
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
                Where-Object { $_.CommandLine -like "*$CommandLinePattern*" } |
                Select-Object -ExpandProperty ProcessId
        )
    } catch {
        return @()
    }
}

function Get-FirstRunningProcess {
    param(
        [int[]]$CandidatePids
    )
    foreach ($candidatePid in ($CandidatePids | Select-Object -Unique)) {
        if ($candidatePid -le 0) { continue }
        try {
            return Get-Process -Id $candidatePid -ErrorAction Stop
        } catch { }
    }
    return $null
}

function Write-ServiceManifest {
    try {
        powershell.exe -NoProfile -File $WriteEndpointsScript -ProjectDir $ProjectDir -Port $Port -BindHost $BindHost 2>&1 | Out-Null
        Write-Log "Service endpoint manifest refreshed"
    } catch {
        Write-Log "Failed to refresh service endpoint manifest"
    }
}

function Wait-WslReady {
    param(
        [int]$MaxAttempts = 30,
        [int]$SleepSeconds = 2
    )
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            $probe = & wsl.exe bash -lc "printf ready" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$probe".Trim() -eq "ready") {
                Write-Log "WSL ready after ${attempt} attempt(s)"
                return $true
            }
        } catch { }
        Start-Sleep -Seconds $SleepSeconds
    }
    return $false
}

function Test-WslPortListening {
    param(
        [int]$ListenPort
    )
    try {
        $probe = & wsl.exe bash -lc "if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | grep -q ':$ListenPort ' && echo ready || echo wait; elif command -v netstat >/dev/null 2>&1; then netstat -ltn 2>/dev/null | grep -q ':$ListenPort ' && echo ready || echo wait; else echo unknown; fi" 2>$null
        return "$probe".Trim() -eq "ready"
    } catch {
        return $false
    }
}

function Start-OpenClawGatewayIfNeeded {
    param(
        [int]$GatewayPort = 18789
    )
    if (Test-WslPortListening -ListenPort $GatewayPort) {
        Write-Log "OpenClaw gateway already listening on WSL port $GatewayPort"
        return $true
    }

    Write-Log "Starting OpenClaw gateway inside WSL..."
    try {
        & wsl.exe bash -lc "cd '$WslProjectDir' && if command -v systemctl >/dev/null 2>&1; then systemctl --user restart openclaw-gateway.service >/dev/null 2>&1 || true; fi; if ! (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ':$GatewayPort '); then nohup ./scripts/start_openclaw_gateway.sh '/tmp/openclaw-gateway.log' >/tmp/openclaw-gateway.bootstrap.log 2>&1 & fi" 2>&1 | Out-Null
    } catch {
        Write-Log "Failed to invoke OpenClaw gateway bootstrap inside WSL"
    }

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        if (Test-WslPortListening -ListenPort $GatewayPort) {
            Write-Log "OpenClaw gateway ready on WSL port $GatewayPort"
            return $true
        }
    }

    Write-Log "WARNING: OpenClaw gateway not ready within 40s"
    return $false
}

Write-Log "=== ashare-system-v2 starting ==="

# Wait for system to be ready
Start-Sleep -Seconds 15

# --- Step 1: Wake WSL ---
Write-Log "Waking WSL..."
wsl.exe --exec echo "ready" 2>&1 | Out-Null
Start-Sleep -Seconds 5
if (-not (Wait-WslReady)) {
    Write-Log "WARNING: WSL did not become ready within expected time"
} else {
    Start-OpenClawGatewayIfNeeded | Out-Null
}

$apiProc = $null
$schedProc = $null
$previousApiPid = 0
$previousSchedPid = 0
if (Test-Path "$StateDir\service.pid") {
    try {
        $svc = Get-Content "$StateDir\service.pid" -Raw | ConvertFrom-Json
        $previousApiPid = [int]($svc.api_pid | ForEach-Object { $_ })
        $previousSchedPid = [int]($svc.sched_pid | ForEach-Object { $_ })
    } catch {
        Write-Log "Failed to parse previous service.pid"
    }
}

$apiProc = Get-FirstRunningProcess -CandidatePids (@($previousApiPid) + (Find-PythonProcessIds -CommandLinePattern "ashare_system.run serve"))
$schedProc = Get-FirstRunningProcess -CandidatePids (@($previousSchedPid) + (Find-PythonProcessIds -CommandLinePattern "ashare_system.run scheduler"))

# --- Step 2: Start ashare FastAPI ---
if (Test-HttpReady -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -Property "status" -ExpectedValue "ok") {
    if ($apiProc) {
        Write-Log "FastAPI already healthy, skip restart (PID=$($apiProc.Id))"
    } else {
        Write-Log "FastAPI already healthy, skip restart"
    }
    Write-ServiceManifest
} else {
    Write-Log "FastAPI unavailable, starting service..."
    if ($previousApiPid -gt 0) {
        Stop-IfRunning -ProcessId $previousApiPid -Label "api"
    }
    try {
            $portUsers = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($portOwnerProcessId in $portUsers) {
            Stop-IfRunning -ProcessId $portOwnerProcessId -Label "port-$Port-holder"
        }
    } catch {
        Write-Log "Failed to inspect port $Port holders"
    }
    Start-Sleep -Seconds 2
    $apiProc = Start-Process `
        -FilePath $Python `
        -ArgumentList "-m ashare_system.run serve --host $BindHost --port $Port" `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput "$LogDir\api_service.log" `
        -RedirectStandardError "$LogDir\api_service.err" `
        -PassThru -WindowStyle Hidden
    Write-Log "FastAPI started PID=$($apiProc.Id) bind=${BindHost}:$Port"

    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -ErrorAction Stop
            if ($resp.status -eq "ok") {
                Write-Log "FastAPI ready: mode=$($resp.mode)"
                $ready = $true
                break
            }
        } catch { }
    }
    if (-not $ready) { Write-Log "WARNING: FastAPI not ready within 60s" }
    if ($ready) {
        Write-ServiceManifest
    }
}

# --- Step 3: Start scheduler ---
if ($NoScheduler) {
    Write-Log "NoScheduler mode enabled. Skip scheduler startup."
} elseif ($schedProc -and -not $schedProc.HasExited) {
    Write-Log "Scheduler already running, skip restart (PID=$($schedProc.Id))"
} else {
    Write-Log "Starting scheduler..."
    if ($previousSchedPid -gt 0) {
        Stop-IfRunning -ProcessId $previousSchedPid -Label "scheduler"
    }
    $schedProc = Start-Process `
        -FilePath $Python `
        -ArgumentList "-m ashare_system.run scheduler" `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput "$LogDir\scheduler.log" `
        -RedirectStandardError "$LogDir\scheduler.err" `
        -PassThru -WindowStyle Hidden
    Write-Log "Scheduler started PID=$($schedProc.Id)"
}

# Save PIDs
@{
    api_pid = if ($apiProc) { $apiProc.Id } else { 0 }
    sched_pid = if ($schedProc) { $schedProc.Id } else { 0 }
    start_time = (Get-Date -Format "s")
} `
    | ConvertTo-Json | Set-Content "$StateDir\service.pid" -Encoding UTF8

Write-Log "All services started. Watchdog running..."

if ($NoWatchdog) {
    Write-Log "NoWatchdog mode enabled. Startup checks completed, exiting foreground script."
    return
}

# --- Watchdog loop ---
$restartCount = 0
while ($true) {
    Start-Sleep -Seconds 60

    # Check FastAPI
        $alive = $false
        try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5 -ErrorAction Stop
            $alive = ($resp.status -eq "ok")
        } catch { }

        if (-not $alive) {
            $restartCount++
            Write-Log "API not responding (restart #$restartCount)..."
            if (-not $apiProc.HasExited) { $apiProc.Kill() }
            Start-Sleep -Seconds 3
        $apiProc = Start-Process `
            -FilePath $Python `
            -ArgumentList "-m ashare_system.run serve --host $BindHost --port $Port" `
            -WorkingDirectory $ProjectDir `
            -RedirectStandardOutput "$LogDir\api_service.log" `
            -RedirectStandardError "$LogDir\api_service.err" `
            -PassThru -WindowStyle Hidden
    }

    # Check scheduler
    if (-not $NoScheduler -and $schedProc.HasExited) {
        Write-Log "Scheduler crashed, restarting..."
        $schedProc = Start-Process `
            -FilePath $Python `
            -ArgumentList "-m ashare_system.run scheduler" `
            -WorkingDirectory $ProjectDir `
            -RedirectStandardOutput "$LogDir\scheduler.log" `
            -RedirectStandardError "$LogDir\scheduler.err" `
            -PassThru -WindowStyle Hidden
    }

    if ((Get-Date).Minute % 10 -eq 0) {
        $status = if ($alive) { "OK" } else { "ERR" }
        Write-Log "Heartbeat: API=$status restarts=$restartCount"
        if ($alive) {
            Write-ServiceManifest
        }
    }
}
