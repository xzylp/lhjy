#!/usr/bin/env bash
# ashare-system-v2 运行测试
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "[ashare-v2] 运行测试..."

if [[ -x ".venv/bin/python" ]]; then
  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONPATH=src
  exec .venv/bin/python -m pytest tests/ -v "$@"
fi

if [[ -f ".venv/Scripts/python.exe" ]]; then
  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONPATH=src
  exec .venv/Scripts/python.exe -m pytest tests/ -v "$@"
fi

if command -v powershell.exe >/dev/null 2>&1; then
  exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PROJECT_DIR/scripts/run_tests.ps1" "$@"
fi

echo "[ashare-v2] 未找到可用的项目测试解释器。" >&2
exit 1
