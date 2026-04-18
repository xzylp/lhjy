#!/usr/bin/env bash
# 正式压测窗口执行顺序脚本
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
INTENT_ID=""
ALLOWED_SYMBOL=""
DO_APPLY=0
CONFIRM_TEXT=""
API_TIMEOUT="${ASHARE_API_TIMEOUT_SECONDS:-120}"
EVIDENCE_TAG=""

usage() {
    cat >&2 <<'EOF'
用法:
  bash scripts/run_go_live_pressure_sequence.sh [options]

选项:
  --trade-date YYYY-MM-DD
  --account-id ACCOUNT_ID
  --intent-id INTENT_ID           必填
  --allowed-symbol SYMBOL         必填，例如 000001.SZ
  --evidence-tag LABEL            可选，默认自动生成
  --apply
  --confirm APPLY

说明:
  - 默认只做到 go-live gate + 单票 preview
  - 只有同时传入 --apply --confirm APPLY 才会继续执行真实 apply
  - 这是“正式压测窗口主线脚本”，不负责生成新候选，不主动 bootstrap runtime
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
        --intent-id)
            INTENT_ID="${2:-}"
            shift 2
            ;;
        --allowed-symbol)
            ALLOWED_SYMBOL="${2:-}"
            shift 2
            ;;
        --evidence-tag)
            EVIDENCE_TAG="${2:-}"
            shift 2
            ;;
        --apply)
            DO_APPLY=1
            shift
            ;;
        --confirm)
            CONFIRM_TEXT="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[go-live-seq] 未知参数: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "${INTENT_ID}" || -z "${ALLOWED_SYMBOL}" ]]; then
    echo "[go-live-seq] 必须同时传入 --intent-id 和 --allowed-symbol" >&2
    usage
    exit 2
fi

if [[ "${DO_APPLY}" -eq 1 && "${CONFIRM_TEXT}" != "APPLY" ]]; then
    echo "[go-live-seq] 真实派发必须显式传入: --apply --confirm APPLY" >&2
    exit 2
fi

if [[ -z "${EVIDENCE_TAG}" ]]; then
    EVIDENCE_TAG="go_live_${TRADE_DATE}_${ALLOWED_SYMBOL}"
fi

echo "[go-live-seq] trade_date=${TRADE_DATE} account_id=${ACCOUNT_ID} intent_id=${INTENT_ID} symbol=${ALLOWED_SYMBOL} mode=$([[ "${DO_APPLY}" -eq 1 ]] && echo apply || echo preview)"
echo "[go-live-seq] evidence_tag=${EVIDENCE_TAG}"

echo "[go-live-seq] 第一步：执行最终总检查"
bash "${SCRIPT_DIR}/check_go_live_gate.sh" "${TRADE_DATE}" "${ACCOUNT_ID}"

echo "[go-live-seq] 第二步：执行单票受控主线"
if [[ "${DO_APPLY}" -eq 1 ]]; then
    bash "${SCRIPT_DIR}/run_controlled_single_apply.sh" \
        --trade-date "${TRADE_DATE}" \
        --account-id "${ACCOUNT_ID}" \
        --intent-id "${INTENT_ID}" \
        --allowed-symbol "${ALLOWED_SYMBOL}" \
        --evidence-tag "${EVIDENCE_TAG}" \
        --apply --confirm APPLY
else
    bash "${SCRIPT_DIR}/run_controlled_single_apply.sh" \
        --trade-date "${TRADE_DATE}" \
        --account-id "${ACCOUNT_ID}" \
        --intent-id "${INTENT_ID}" \
        --allowed-symbol "${ALLOWED_SYMBOL}" \
        --evidence-tag "${EVIDENCE_TAG}"
fi

echo "[go-live-seq] 完成。若本次为 apply=true，请继续核对 gateway claim / receipt / 飞书执行回执 / QMT 对账。"
