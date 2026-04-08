param(
    [string]$ProjectDir = "D:\Coding\lhjy\ashare-system-v2",
    [int]$Port = 8100,
    [switch]$KeepServicesAlive,
    [switch]$Json
)

$ServiceScript = Join-Path $ProjectDir "scripts\windows_service.ps1"

function Write-Log {
    param([string]$Message)
    if (-not $Json) {
        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Write-Host "[$ts] $Message"
    }
}

function Find-WatchdogProcesses {
    try {
        @(Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -like "*start_unattended.ps1*" })
    } catch {
        @()
    }
}

function Stop-Watchdog {
    $stopped = @()
    foreach ($proc in Find-WatchdogProcesses) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            $stopped += [int]$proc.ProcessId
            Write-Log "Stopped watchdog PID=$($proc.ProcessId)"
        } catch {
            Write-Log "Failed to stop watchdog PID=$($proc.ProcessId): $($_.Exception.Message)"
        }
    }
    return @($stopped)
}

$watchdogPids = Stop-Watchdog
$servicePayload = $null

if (-not $KeepServicesAlive -and (Test-Path $ServiceScript)) {
    try {
        $servicePayload = & $ServiceScript -Action stop -ProjectDir $ProjectDir -Port $Port -Json | ConvertFrom-Json -Depth 6
    } catch {
        Write-Log "Failed to stop services through windows_service.ps1"
    }
}

$payload = [ordered]@{
    ok = $true
    project_dir = $ProjectDir
    keep_services_alive = [bool]$KeepServicesAlive
    watchdog_stopped = @($watchdogPids)
    watchdog_count = @($watchdogPids).Count
    services = $servicePayload
    updated_at = (Get-Date).ToString("s")
}

if ($Json) {
    $payload | ConvertTo-Json -Depth 6
} else {
    Write-Log "Watchdog stopped count=$($payload.watchdog_count)"
    if ($KeepServicesAlive) {
        Write-Log "Service stop skipped by request"
    }
}
