#!/usr/bin/env bash
# 安装 ashare-system-v2 的 Linux systemd 服务
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

SERVICE_NAME="${SERVICE_NAME:-ashare-system-v2.service}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
SYSTEMD_UNIT_PATH="${SYSTEMD_UNIT_PATH:-/etc/systemd/system/${SERVICE_NAME}}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
PYTHON_BIN="${ASHARE_PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[ashare-v2] 缺少环境文件: ${ENV_FILE}" >&2
    exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "[ashare-v2] 缺少 Python 解释器: ${PYTHON_BIN}" >&2
    exit 1
fi

TMP_UNIT="$(mktemp)"
trap 'rm -f "${TMP_UNIT}"' EXIT

cat > "${TMP_UNIT}" <<EOF
[Unit]
Description=ashare-system-v2 Linux control plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONPATH=${PROJECT_DIR}/src
ExecStart=${PYTHON_BIN} -m ashare_system.run serve
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
EOF

echo "[ashare-v2] 安装 systemd unit: ${SYSTEMD_UNIT_PATH}"
sudo install -D -m 644 "${TMP_UNIT}" "${SYSTEMD_UNIT_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
echo "[ashare-v2] 已启用开机自启: ${SERVICE_NAME}"
echo "[ashare-v2] 可执行:"
echo "  sudo systemctl start ${SERVICE_NAME}"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
