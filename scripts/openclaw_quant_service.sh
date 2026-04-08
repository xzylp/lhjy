#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"
PROFILE="quant"
PORT="18890"
UNIT="openclaw-gateway-quant.service"

case "$ACTION" in
  start)
    systemctl --user daemon-reload
    systemctl --user start "$UNIT"
    openclaw --profile "$PROFILE" gateway status
    ;;
  stop)
    systemctl --user stop "$UNIT"
    ;;
  restart)
    systemctl --user daemon-reload
    systemctl --user restart "$UNIT"
    openclaw --profile "$PROFILE" gateway status
    ;;
  status)
    openclaw --profile "$PROFILE" gateway status
    ;;
  config)
    openclaw --profile "$PROFILE" config file
    ;;
  validate)
    openclaw --profile "$PROFILE" config validate
    ;;
  dashboard)
    echo "Quant dashboard: http://127.0.0.1:${PORT}/"
    openclaw --profile "$PROFILE" dashboard
    ;;
  *)
    cat >&2 <<'EOF'
Usage:
  openclaw_quant_service.sh start
  openclaw_quant_service.sh stop
  openclaw_quant_service.sh restart
  openclaw_quant_service.sh status
  openclaw_quant_service.sh config
  openclaw_quant_service.sh validate
  openclaw_quant_service.sh dashboard
EOF
    exit 2
    ;;
esac
