#!/usr/bin/env bash
# OpenClaw gateway systemd 服务管理
set -euo pipefail

ACTION="${1:-status}"
SERVICE_NAME="${SERVICE_NAME:-openclaw-gateway.service}"

case "${ACTION}" in
  start)
    sudo systemctl start "${SERVICE_NAME}"
    sudo systemctl --no-pager --full status "${SERVICE_NAME}"
    ;;
  stop)
    sudo systemctl stop "${SERVICE_NAME}"
    ;;
  restart)
    sudo systemctl restart "${SERVICE_NAME}"
    sudo systemctl --no-pager --full status "${SERVICE_NAME}"
    ;;
  status)
    sudo systemctl --no-pager --full status "${SERVICE_NAME}"
    ;;
  logs)
    sudo journalctl -u "${SERVICE_NAME}" -n "${2:-100}" --no-pager
    ;;
  enable)
    sudo systemctl enable "${SERVICE_NAME}"
    ;;
  disable)
    sudo systemctl disable "${SERVICE_NAME}"
    ;;
  *)
    cat >&2 <<'EOF'
Usage:
  openclaw_gateway_service.sh start
  openclaw_gateway_service.sh stop
  openclaw_gateway_service.sh restart
  openclaw_gateway_service.sh status
  openclaw_gateway_service.sh logs [line_count]
  openclaw_gateway_service.sh enable
  openclaw_gateway_service.sh disable
EOF
    exit 2
    ;;
esac
