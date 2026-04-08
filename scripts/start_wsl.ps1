# ashare-system-v2 WSL 无人值守启动脚本
# 功能: 通过 wsl.exe 启动 WSL 内的 FastAPI + 调度器，崩溃自动重启
# 用法: 在 Windows 任务计划程序中配置此脚本开机运行 (不需要管理员权限)

$ProjectDir = "D:\Coding\lhjy\ashare-system-v2"
$WslProjectDir = "/mnt/d/Coding/lhjy/ashare-system-v2"
$LogDir = "$ProjectDir\logs"
$StateDir = "$ProjectDir\.ashare_state"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Write-Log {
    param($Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $Message"
    Write-Host $line
    Add-Content -Path "$LogDir\startup.log" -Value $line -Encoding UTF8
}

Write-Log "=== ashare-system-v2 WSL 启动 ==="

# 等待系统网络就绪
Start-Sleep -Seconds 15

# 先唤醒 WSL 实例 (防止 WSL 还没初始化)
Write-Log "唤醒 WSL..."
wsl.exe --exec echo "WSL ready" 2>&1 | Out-Null
Start-Sleep -Seconds 3

# 启动 FastAPI 服务 (后台运行，日志写入文件)
Write-Log "启动 FastAPI 服务..."
$apiJob = Start-Job -ScriptBlock {
    param($WslDir, $LogDir)
    wsl.exe bash -lc "cd '$WslDir' && source .venv/bin/activate && nohup python -m ashare_system.run serve >> '$WslDir/logs/api_service.log' 2>&1 &"
} -ArgumentList $WslProjectDir, $LogDir

Wait-Job $apiJob -Timeout 30 | Out-Null
Remove-Job $apiJob -Force -ErrorAction SilentlyContinue

Write-Log "等待 FastAPI 就绪..."
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8100/health" -TimeoutSec 3 -ErrorAction Stop
        if ($resp.status -eq "ok") {
            Write-Log "FastAPI 就绪: mode=$($resp.mode)"
            $ready = $true
            break
        }
    } catch { }
}

if (-not $ready) {
    Write-Log "警告: FastAPI 未能在60秒内就绪"
}

# 启动调度器
Write-Log "启动调度器..."
$schedJob = Start-Job -ScriptBlock {
    param($WslDir)
    wsl.exe bash -lc "cd '$WslDir' && source .venv/bin/activate && nohup python -m ashare_system.run scheduler >> '$WslDir/logs/scheduler.log' 2>&1 &"
} -ArgumentList $WslProjectDir

Wait-Job $schedJob -Timeout 15 | Out-Null
Remove-Job $schedJob -Force -ErrorAction SilentlyContinue

Write-Log "所有服务已启动，进入守护模式..."

# 守护循环: 每60秒检查 API 是否存活，挂了就重启
$restartCount = 0
while ($true) {
    Start-Sleep -Seconds 60

    $alive = $false
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8100/health" -TimeoutSec 5 -ErrorAction Stop
        $alive = ($resp.status -eq "ok")
    } catch { }

    if (-not $alive) {
        $restartCount++
        Write-Log "API 服务无响应 (第${restartCount}次)，重启..."
        wsl.exe bash -lc "cd '$WslProjectDir' && source .venv/bin/activate && nohup python -m ashare_system.run serve >> '$WslProjectDir/logs/api_service.log' 2>&1 &"
        Start-Sleep -Seconds 15
    }

    # 每10分钟心跳日志
    if ((Get-Date).Minute % 10 -eq 0) {
        $status = if ($alive) { "OK" } else { "RESTARTING" }
        Write-Log "心跳: API=$status 重启次数=$restartCount"
    }
}
