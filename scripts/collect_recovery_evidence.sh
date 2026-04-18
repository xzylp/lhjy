#!/usr/bin/env bash
# 收集服务重启 / 断链恢复演练证据
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
source "$SCRIPT_DIR/common_env.sh"

if [[ -f ".env" ]]; then
    set -a
    source ".env"
    set +a
fi

TRADE_DATE="$(date +%F)"
ACCOUNT_ID="${ASHARE_ACCOUNT_ID:-8890130545}"
TAG="snapshot"
API_TIMEOUT="${ASHARE_API_TIMEOUT_SECONDS:-60}"
OUTPUT_ROOT="${ASHARE_RECOVERY_EVIDENCE_ROOT:-$PROJECT_DIR/logs/recovery_evidence}"
PYTHON_BIN="$(resolve_project_python || true)"

usage() {
    cat >&2 <<'EOF'
用法:
  bash scripts/collect_recovery_evidence.sh [options]

选项:
  --trade-date YYYY-MM-DD
  --account-id ACCOUNT_ID
  --tag LABEL
  --output-root PATH
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trade-date)
            TRADE_DATE="${2:-}"
            shift 2
            ;;
        --account-id)
            ACCOUNT_ID="${2:-}"
            shift 2
            ;;
        --tag)
            TAG="${2:-}"
            shift 2
            ;;
        --output-root)
            OUTPUT_ROOT="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[recovery-evidence] 未知参数: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[recovery-evidence] 未找到可用 Python 解释器，请先配置 .venv 或 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SAFE_TAG="$(printf '%s' "${TAG}" | tr ' /' '__')"
OUT_DIR="${OUTPUT_ROOT}/${TIMESTAMP}_${SAFE_TAG}"
mkdir -p "${OUT_DIR}"

fetch_to_file() {
    local route="$1"
    local filename="$2"
    if ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" get "$route" > "${OUT_DIR}/${filename}.json" 2>"${OUT_DIR}/${filename}.err"; then
        return 0
    fi
    printf '{"ok":false,"error":"fetch_failed","route":"%s"}\n' "$route" > "${OUT_DIR}/${filename}.json"
}

run_text_capture() {
    local filename="$1"
    shift
    if "$@" > "${OUT_DIR}/${filename}.txt" 2>"${OUT_DIR}/${filename}.err"; then
        return 0
    fi
    {
        printf 'capture_failed\n'
        printf 'command='
        printf '%q ' "$@"
        printf '\n'
    } > "${OUT_DIR}/${filename}.txt"
}

fetch_to_file "/health" "health"
fetch_to_file "/system/operations/components" "operations_components"
fetch_to_file "/system/readiness?account_id=${ACCOUNT_ID}" "readiness"
fetch_to_file "/system/deployment/service-recovery-readiness?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}&include_details=false" "service_recovery_readiness"
fetch_to_file "/system/deployment/controlled-apply-readiness?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}&require_live=false&require_trading_session=false&include_details=false" "controlled_apply_readiness"
fetch_to_file "/system/feishu/longconn/status" "feishu_longconn_status"
fetch_to_file "/system/workspace-context" "workspace_context"
fetch_to_file "/system/agents/supervision-board?trade_date=${TRADE_DATE}" "supervision_board"
fetch_to_file "/system/feishu/briefing?trade_date=${TRADE_DATE}" "feishu_briefing"
fetch_to_file "/system/discussions/execution-dispatch/latest?trade_date=${TRADE_DATE}" "execution_dispatch_latest"
fetch_to_file "/monitor/state" "monitor_state"
run_text_capture "feishu_longconn_service_status" systemctl --user status ashare-feishu-longconn.service --no-pager
run_text_capture "feishu_longconn_verify" bash "$SCRIPT_DIR/ashare_feishu_longconn_ctl.sh" --user verify

"${PYTHON_BIN}" - "${OUT_DIR}" "${TRADE_DATE}" "${ACCOUNT_ID}" "${TAG}" <<'PY'
import json
import sys
from pathlib import Path


out_dir = Path(sys.argv[1])
trade_date = sys.argv[2]
account_id = sys.argv[3]
tag = sys.argv[4]


def load(name: str) -> dict:
    path = out_dir / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "error": "invalid_json", "name": name}


health = load("health")
components = load("operations_components")
recovery = load("service_recovery_readiness")
apply_ready = load("controlled_apply_readiness")
longconn = load("feishu_longconn_status")


def summarize_status(payload: dict, *, status_key: str = "status") -> str:
    value = payload.get(status_key)
    if value not in (None, ""):
        return str(value)
    error = payload.get("error")
    if error:
        return f"error:{error}"
    if payload.get("ok") is False:
        return "error:unknown"
    return "missing"


def load_text(name: str) -> str:
    path = out_dir / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""

summary_lines = [
    f"tag={tag} trade_date={trade_date} account_id={account_id}",
    f"health={summarize_status(health)}",
    f"recovery={summarize_status(recovery)}",
    f"apply_ready={summarize_status(apply_ready)}",
    f"longconn={summarize_status(longconn)} fresh={longconn.get('is_fresh')}",
]

component_items = components.get("components") or []
if component_items:
    compact = "；".join(f"{item.get('name')}={item.get('status')}" for item in component_items)
    summary_lines.append(f"components={compact}")

feishu_status_text = load_text("feishu_longconn_service_status")
if feishu_status_text:
    summary_lines.append(f"feishu_service_status={feishu_status_text.splitlines()[0].strip()}")

feishu_verify_text = load_text("feishu_longconn_verify")
if feishu_verify_text:
    summary_lines.append(f"feishu_verify={feishu_verify_text.splitlines()[0].strip()}")

(out_dir / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
print(f"[recovery-evidence] 已写入 {out_dir}")
for line in summary_lines:
    print(f"- {line}")
PY
