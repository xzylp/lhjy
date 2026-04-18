#!/usr/bin/env bash

set -euo pipefail

cd "/srv/projects/ashare-system-v2"
source "/srv/projects/ashare-system-v2/scripts/common_env.sh"

exec "${ASHARE_PYTHON_BIN:-/srv/projects/ashare-system-v2/.venv/bin/python}" -m ashare_system.run feishu-longconn
