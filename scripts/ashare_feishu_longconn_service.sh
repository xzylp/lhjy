#!/usr/bin/env bash

set -euo pipefail

cd "/srv/projects/ashare-system-v2"
source "/srv/projects/ashare-system-v2/scripts/common_env.sh"

BOT_ROLE="${ASHARE_FEISHU_LONGCONN_BOT_ROLE:-${ASHARE_FEISHU_BOT_ROLE:-main}}"

exec "${ASHARE_PYTHON_BIN:-/srv/projects/ashare-system-v2/.venv/bin/python}" -m ashare_system.run feishu-longconn --bot-role "${BOT_ROLE}"
