#!/usr/bin/env bash
# 安装 ashare-system-v2 飞书长连接 worker 的 Linux systemd 服务
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

SERVICE_SCOPE="${SERVICE_SCOPE:-user}"
if [[ "${1:-}" == "--system" ]]; then
    SERVICE_SCOPE="system"
    shift
elif [[ "${1:-}" == "--user" ]]; then
    SERVICE_SCOPE="user"
    shift
fi

SERVICE_NAME="${SERVICE_NAME:-ashare-feishu-longconn.service}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
SERVICE_SCRIPT="${SERVICE_SCRIPT:-${PROJECT_DIR}/scripts/ashare_feishu_longconn_service.sh}"

if [[ "${SERVICE_SCOPE}" == "user" ]]; then
    SYSTEMD_UNIT_PATH="${SYSTEMD_UNIT_PATH:-${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user/${SERVICE_NAME}}"
    SYSTEMCTL_CMD=(systemctl --user)
    WANTED_BY="default.target"
    PREFIX="[ashare-feishu-longconn:user]"
    UNIT_AFTER="network-online.target"
    USER_GROUP_BLOCK=""
else
    SYSTEMD_UNIT_PATH="${SYSTEMD_UNIT_PATH:-/etc/systemd/system/${SERVICE_NAME}}"
    SYSTEMCTL_CMD=(sudo systemctl)
    WANTED_BY="multi-user.target"
    PREFIX="[ashare-feishu-longconn:system]"
    UNIT_AFTER="network-online.target ashare-system-v2.service"
    USER_GROUP_BLOCK=$'User='"${SERVICE_USER}"$'\nGroup='"${SERVICE_GROUP}"
fi

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "${PREFIX} 缺少环境文件: ${ENV_FILE}" >&2
    exit 1
fi

if [[ ! -x "${SERVICE_SCRIPT}" ]]; then
    echo "${PREFIX} 缺少可执行启动脚本: ${SERVICE_SCRIPT}" >&2
    exit 1
fi

mkdir -p "$(dirname "${SYSTEMD_UNIT_PATH}")"

TMP_UNIT="$(mktemp)"
trap 'rm -f "${TMP_UNIT}"' EXIT

cat > "${TMP_UNIT}" <<EOF
[Unit]
Description=ashare-system-v2 Feishu long connection worker
After=${UNIT_AFTER}
Wants=network-online.target

[Service]
Type=simple
${USER_GROUP_BLOCK}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${PROJECT_DIR}/src
ExecStart=${SERVICE_SCRIPT}
Restart=always
RestartSec=5
StartLimitIntervalSec=120
StartLimitBurst=20
TimeoutStopSec=20
KillSignal=SIGINT
NoNewPrivileges=true

[Install]
WantedBy=${WANTED_BY}
EOF

echo "${PREFIX} 安装 systemd unit: ${SYSTEMD_UNIT_PATH}"
if [[ "${SERVICE_SCOPE}" == "user" ]]; then
    install -D -m 644 "${TMP_UNIT}" "${SYSTEMD_UNIT_PATH}"
else
    sudo install -D -m 644 "${TMP_UNIT}" "${SYSTEMD_UNIT_PATH}"
fi
"${SYSTEMCTL_CMD[@]}" daemon-reload
"${SYSTEMCTL_CMD[@]}" enable "${SERVICE_NAME}"
echo "${PREFIX} 已启用开机自启: ${SERVICE_NAME}"
echo "${PREFIX} 可执行:"
if [[ "${SERVICE_SCOPE}" == "user" ]]; then
    echo "  systemctl --user start ${SERVICE_NAME}"
    echo "  systemctl --user status ${SERVICE_NAME}"
    echo "  journalctl --user -u ${SERVICE_NAME} -n 100 --no-pager"
    echo "  如需开机后无人登录也自动运行，请执行: sudo loginctl enable-linger ${SERVICE_USER}"
else
    echo "  sudo systemctl start ${SERVICE_NAME}"
    echo "  sudo systemctl status ${SERVICE_NAME}"
    echo "  sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
fi
