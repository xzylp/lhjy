#!/usr/bin/env bash
# 安装 OpenClaw gateway 的 Linux systemd 服务
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

SERVICE_NAME="${SERVICE_NAME:-openclaw-gateway.service}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
SYSTEMD_UNIT_PATH="${SYSTEMD_UNIT_PATH:-/etc/systemd/system/${SERVICE_NAME}}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
OPENCLAW_BIN="${OPENCLAW_BIN:-/home/yxz/.npm-global/bin/openclaw}"
LOG_FILE="${OPENCLAW_GATEWAY_LOG_FILE:-${PROJECT_DIR}/logs/openclaw-gateway.log}"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[openclaw] 缺少环境文件: ${ENV_FILE}" >&2
    exit 1
fi

if [[ ! -x "${OPENCLAW_BIN}" ]]; then
    echo "[openclaw] 缺少 openclaw 可执行文件: ${OPENCLAW_BIN}" >&2
    exit 1
fi

mkdir -p "$(dirname "${LOG_FILE}")"

TMP_UNIT="$(mktemp)"
trap 'rm -f "${TMP_UNIT}"' EXIT

cat > "${TMP_UNIT}" <<EOF
[Unit]
Description=OpenClaw gateway for ashare-system-v2
After=network-online.target ashare-system-v2.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${PROJECT_DIR}/scripts/start_openclaw_gateway.sh ${LOG_FILE}
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
EOF

echo "[openclaw] 安装 systemd unit: ${SYSTEMD_UNIT_PATH}"
sudo install -D -m 644 "${TMP_UNIT}" "${SYSTEMD_UNIT_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
echo "[openclaw] 已启用开机自启: ${SERVICE_NAME}"
echo "[openclaw] 可执行:"
echo "  sudo systemctl start ${SERVICE_NAME}"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
