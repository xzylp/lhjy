param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status",
    [string]$ProjectDir = "D:\Coding\lhjy\ashare-system-v2",
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8100,
    [switch]$NoScheduler,
    [switch]$Json
)

$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectDir "logs"
$StateDir = Join-Path $ProjectDir ".ashare_state"
$ApiOut = Join-Path $LogDir "api_service.log"
$ApiErr = Join-Path $LogDir "api_service.err"
$SchedOut = Join-Path $LogDir "scheduler.log"
$SchedErr = Join-Path $LogDir "scheduler.err"
$StateFile = Join-Path $StateDir "service.pid"
$WriteEndpointsScript = Join-Path $ProjectDir "scripts\write_service_endpoints.ps1"
$ServiceManifestPath = Join-Path $StateDir "service_endpoints.json"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    if (-not $Json) {
        Write-Host "[$ts] $Message"
    }
}

function Find-ServiceProcesses {
    try {
        @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
            Where-Object {
                $_.CommandLine -like "*ashare_system.run serve*" -or
                $_.CommandLine -like "*ashare_system.run scheduler*"
            })
    } catch {
        @()
    }
}

function Find-ApiProcesses {
    Find-ServiceProcesses | Where-Object { $_.CommandLine -like "*ashare_system.run serve*" }
}

function Find-SchedulerProcesses {
    Find-ServiceProcesses | Where-Object { $_.CommandLine -like "*ashare_system.run scheduler*" }
}

function Find-WatchdogProcesses {
    try {
        @(Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -like "*start_unattended.ps1*" })
    } catch {
        @()
    }
}

function Stop-ProcessList {
    param([array]$Processes)
    foreach ($proc in $Processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Log "Stopped PID=$($proc.ProcessId)"
        } catch {
            Write-Log "Failed to stop PID=$($proc.ProcessId): $($_.Exception.Message)"
        }
    }
}

function Test-Health {
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -ErrorAction Stop
        return $resp
    } catch {
        return $null
    }
}

function Write-ServiceManifest {
    if (-not (Test-Path $WriteEndpointsScript)) {
        return
    }
    try {
        & $WriteEndpointsScript -ProjectDir $ProjectDir -Port $Port -BindHost $BindHost | Out-Null
    } catch { }
}

function Save-State {
    $payload = @{
        api_pid = ((Find-ApiProcesses | Select-Object -First 1).ProcessId)
        sched_pid = ((Find-SchedulerProcesses | Select-Object -First 1).ProcessId)
        updated_at = (Get-Date -Format "s")
    }
    $payload | ConvertTo-Json | Set-Content $StateFile -Encoding UTF8
}

function Start-Api {
    $health = Test-Health
    if ($health -and $health.status -eq "ok") {
        Write-Log "API already healthy: mode=$($health.mode)"
        return
    }
    $existing = Find-ApiProcesses
    if ($existing.Count -gt 0) {
        Stop-ProcessList -Processes $existing
        Start-Sleep -Seconds 2
    }
    $apiProc = Start-Process `
        -FilePath $Python `
        -ArgumentList "-m ashare_system.run serve --host $BindHost --port $Port" `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $ApiOut `
        -RedirectStandardError $ApiErr `
        -PassThru `
        -WindowStyle Hidden
    Write-Log "Started API PID=$($apiProc.Id) bind=${BindHost}:$Port"

    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        $health = Test-Health
        if ($health -and $health.status -eq "ok") {
            Write-Log "API healthy: mode=$($health.mode)"
            Write-ServiceManifest
            return
        }
    }
    Write-Log "API health check timed out"
}

function Start-Scheduler {
    if ($NoScheduler) {
        Write-Log "Skip scheduler by request"
        return
    }
    $existing = Find-SchedulerProcesses
    if ($existing.Count -gt 0) {
        Write-Log "Scheduler already running PID=$($existing[0].ProcessId)"
        return
    }
    $schedProc = Start-Process `
        -FilePath $Python `
        -ArgumentList "-m ashare_system.run scheduler" `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $SchedOut `
        -RedirectStandardError $SchedErr `
        -PassThru `
        -WindowStyle Hidden
    Write-Log "Started scheduler PID=$($schedProc.Id)"
}

function Show-Status {
    $payload = Get-StatusPayload
    $health = $payload.health
    $apiPids = $payload.processes.api_pids
    $schedPids = $payload.processes.scheduler_pids
    if ($health) {
        Write-Log "Health: status=$($health.status) mode=$($health.mode) service=$($health.service)"
    } else {
        Write-Log "Health: unavailable"
    }
    if ($apiPids.Count -gt 0) {
        Write-Log "API PIDs: $(($apiPids) -join ', ')"
    } else {
        Write-Log "API PIDs: none"
    }
    if ($schedPids.Count -gt 0) {
        Write-Log "Scheduler PIDs: $(($schedPids) -join ', ')"
    } else {
        Write-Log "Scheduler PIDs: none"
    }
}

function Get-StatusPayload {
    $health = Test-Health
    $api = @(Find-ApiProcesses | Select-Object -ExpandProperty ProcessId)
    $sched = @(Find-SchedulerProcesses | Select-Object -ExpandProperty ProcessId)
    $watchdog = @(Find-WatchdogProcesses | Select-Object -ExpandProperty ProcessId)
    return [ordered]@{
        ok = $true
        action = $Action
        project_dir = $ProjectDir
        bind_host = $BindHost
        port = $Port
        health = if ($health) { $health } else { @{ status = "unavailable"; mode = ""; service = "" } }
        processes = @{
            api_pids = @($api)
            scheduler_pids = @($sched)
            watchdog_pids = @($watchdog)
        }
        manifests = @{
            service_endpoints = $ServiceManifestPath
            service_state = $StateFile
        }
        logs = @{
            startup = (Join-Path $LogDir "startup.log")
            api_stdout = $ApiOut
            api_stderr = $ApiErr
            scheduler_stdout = $SchedOut
            scheduler_stderr = $SchedErr
        }
        updated_at = (Get-Date).ToString("s")
    }
}

switch ($Action) {
    "start" {
        Start-Api
        Start-Scheduler
        Save-State
        if ($Json) {
            Get-StatusPayload | ConvertTo-Json -Depth 6
        } else {
            Show-Status
        }
    }
    "stop" {
        Stop-ProcessList -Processes (Find-ServiceProcesses)
        Save-State
        if ($Json) {
            Get-StatusPayload | ConvertTo-Json -Depth 6
        } else {
            Show-Status
        }
    }
    "restart" {
        Stop-ProcessList -Processes (Find-ServiceProcesses)
        Start-Sleep -Seconds 2
        Start-Api
        Start-Scheduler
        Save-State
        if ($Json) {
            Get-StatusPayload | ConvertTo-Json -Depth 6
        } else {
            Show-Status
        }
    }
    "status" {
        if ($Json) {
            Get-StatusPayload | ConvertTo-Json -Depth 6
        } else {
            Show-Status
        }
    }
}
