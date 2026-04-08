param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status",
    [string]$ProjectDir = "D:\Coding\lhjy\ashare-system-v2",
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8100,
    [switch]$NoScheduler,
    [switch]$Json
)

$ManualScript = Join-Path $ProjectDir "scripts\manual_service.ps1"

if (-not (Test-Path $ManualScript)) {
    Write-Error "manual service script not found: $ManualScript"
    exit 1
}

$params = @{
    Action = $Action
    ProjectDir = $ProjectDir
    BindHost = $BindHost
    Port = $Port
}

if ($NoScheduler) {
    $params["NoScheduler"] = $true
}
if ($Json) {
    $params["Json"] = $true
}

& $ManualScript @params
exit $LASTEXITCODE
