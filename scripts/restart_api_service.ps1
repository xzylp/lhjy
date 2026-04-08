param(
    [string]$ProjectDir = "D:\Coding\lhjy\ashare-system-v2",
    [int]$Port = 8100,
    [string]$BindHost = "0.0.0.0"
)

$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$logDir = Join-Path $ProjectDir "logs"
$writeEndpointsScript = Join-Path $ProjectDir "scripts\write_service_endpoints.ps1"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Stop-ByProcessId {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return }
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        Write-Host "Stopped PID=$ProcessId"
    } catch { }
}

function Find-ServicePids {
    try {
        return @(
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
                Where-Object { $_.CommandLine -like "*ashare_system.run serve*" } |
                Select-Object -ExpandProperty ProcessId
        )
    } catch {
        return @()
    }
}

$listenPids = @()
try {
    $listenPids = @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
} catch { }

$allPids = @($listenPids + (Find-ServicePids)) | Where-Object { $_ -gt 0 } | Select-Object -Unique
foreach ($procId in $allPids) {
    Stop-ByProcessId -ProcessId $procId
}

Start-Sleep -Seconds 2

$proc = Start-Process `
    -FilePath $python `
    -ArgumentList "-m ashare_system.run serve --host $BindHost --port $Port" `
    -WorkingDirectory $ProjectDir `
    -RedirectStandardOutput (Join-Path $logDir "api_service.log") `
    -RedirectStandardError (Join-Path $logDir "api_service.err") `
    -PassThru `
    -WindowStyle Hidden

Start-Sleep -Seconds 2
try {
    powershell.exe -NoProfile -File $writeEndpointsScript -ProjectDir $ProjectDir -Port $Port -BindHost $BindHost 2>&1 | Out-Null
} catch { }

Write-Host "Started API PID=$($proc.Id) bind=${BindHost}:$Port"
