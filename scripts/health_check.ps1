# Health check script for ashare-system-v2

$Python = "D:\Coding\lhjy\ashare-system-v2\.venv\Scripts\python.exe"
$ProjectDir = "D:\Coding\lhjy\ashare-system-v2"

function Test-Api {
    param($Url, $Name)
    try {
        $resp = Invoke-RestMethod -Uri $Url -TimeoutSec 5 -ErrorAction Stop
        Write-Host "[OK] $Name"
        return $true
    } catch {
        Write-Host "[FAIL] $Name : $($_.Exception.Message)"
        return $false
    }
}

Write-Host "=== ashare-system-v2 health check $(Get-Date -Format 'HH:mm:ss') ==="

$ok = $true
$ok = (Test-Api "http://127.0.0.1:8100/health"            "system") -and $ok
$ok = (Test-Api "http://127.0.0.1:8100/execution/health"  "execution") -and $ok
$ok = (Test-Api "http://127.0.0.1:8100/market/health"     "market") -and $ok
$ok = (Test-Api "http://127.0.0.1:8100/system/config"     "runtime_config") -and $ok

# Check QMT process
$qmt = Get-Process -Name "XtMiniQmt" -ErrorAction SilentlyContinue
if ($qmt) {
    Write-Host "[OK] QMT process PID=$($qmt.Id)"
} else {
    Write-Host "[FAIL] QMT process not running"
    $ok = $false
}

# Check account balance
try {
    $balance = Invoke-RestMethod -Uri "http://127.0.0.1:8100/execution/balance/8890130545" -TimeoutSec 5
    Write-Host "[OK] balance total=$($balance.total_asset) cash=$($balance.cash)"
} catch {
    Write-Host "[WARN] balance unavailable (mock mode?)"
}

# Check runtime config
try {
    $cfg = Invoke-RestMethod -Uri "http://127.0.0.1:8100/system/config" -TimeoutSec 5
    Write-Host "[OK] config max_buy=$($cfg.max_buy_count) scope_main=$($cfg.scope.allow_main_board)"
} catch {
    Write-Host "[WARN] config unavailable"
}

Write-Host ""
if ($ok) {
    Write-Host "[PASS] All services healthy"
    exit 0
} else {
    Write-Host "[FAIL] Some services down - check logs: $ProjectDir\logs\"
    exit 1
}
