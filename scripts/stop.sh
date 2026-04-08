#!/usr/bin/env bash
# ashare-system-v2 停止服务
set -euo pipefail

echo "[ashare-v2] 停止服务..."

# 查找并终止 uvicorn 进程
if pgrep -f "ashare_system.run serve" > /dev/null 2>&1; then
    pkill -f "ashare_system.run serve"
    echo "[ashare-v2] 服务已停止"
else
    echo "[ashare-v2] 未发现运行中的服务"
fi
