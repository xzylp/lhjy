#!/usr/bin/env bash
# ashare-system-v2 scheduler systemd 服务管理
set -euo pipefail

ACTION="${1:-status}"
SERVICE_NAME="${SERVICE_NAME:-ashare-system-v2-scheduler.service}"

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
  ashare_scheduler_service.sh start
  ashare_scheduler_service.sh stop
  ashare_scheduler_service.sh restart
  ashare_scheduler_service.sh status
  ashare_scheduler_service.sh logs [line_count]
  ashare_scheduler_service.sh enable
  ashare_scheduler_service.sh disable
EOF
    exit 2
    ;;
esac
