#!/usr/bin/env bash
# ashare-system-v2 本地 API 客户端
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
source "$SCRIPT_DIR/common_env.sh"

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

normalize_service_host() {
    local raw_host="${1:-}"
    case "$raw_host" in
        ""|"0.0.0.0"|"::"|"[::]")
            printf '127.0.0.1\n'
            ;;
        *)
            printf '%s\n' "$raw_host"
            ;;
    esac
}

resolve_base_url() {
    local explicit_url="${ASHARE_API_BASE_URL:-}"
    if [[ -n "$explicit_url" ]]; then
        printf '%s\n' "${explicit_url%/}"
        return 0
    fi

    # 如果启用了 Go 平台，优先重定向到 Go 平台端口 (18793)
    if [[ "${ASHARE_GO_PLATFORM_ENABLED:-false}" == "true" ]]; then
        local go_url="${ASHARE_GO_PLATFORM_BASE_URL:-}"
        if [[ -n "$go_url" ]]; then
            printf '%s\n' "${go_url%/}"
            return 0
        fi
        printf 'http://127.0.0.1:18793\n'
        return 0
    fi

    local wsl_url="${ASHARE_WSL_SERVICE_URL:-}"
    if [[ -n "$wsl_url" ]]; then
        printf '%s\n' "${wsl_url%/}"
        return 0
    fi

    local host
    host="$(normalize_service_host "${ASHARE_SERVICE_HOST:-127.0.0.1}")"
    local port="${ASHARE_SERVICE_PORT:-8100}"
    printf 'http://%s:%s\n' "$host" "$port"
}

join_url() {
    local base_url="$1"
    local route="$2"
    if [[ "$route" =~ ^https?:// ]]; then
        printf '%s\n' "$route"
        return 0
    fi

    if [[ "$route" == /* ]]; then
        printf '%s%s\n' "$base_url" "$route"
        return 0
    fi

    printf '%s/%s\n' "$base_url" "$route"
}

curl_json() {
    local method="$1"
    local url="$2"
    shift 2
    curl --fail --silent --show-error \
        --max-time "${ASHARE_API_TIMEOUT_SECONDS:-30}" \
        -X "$method" \
        -H "Accept: application/json" \
        "$@" \
        "$url"
}

usage() {
    cat >&2 <<'EOF'
用法:
  scripts/ashare_api.sh probe
  scripts/ashare_api.sh get /health
  scripts/ashare_api.sh post /runtime/jobs/pipeline '{}'
  scripts/ashare_api.sh put /system/params/proposals @/tmp/payload.json
  scripts/ashare_api.sh delete /system/cases/{case_id}
EOF
}

main() {
    local action="${1:-}"
    if [[ -z "$action" ]]; then
        usage
        exit 2
    fi

    local base_url
    base_url="$(resolve_base_url)"

    case "${action,,}" in
        probe)
            printf 'ASHARE_WSL_SERVICE_URL=%s\n' "$base_url"
            curl_json GET "$(join_url "$base_url" "/health")" >/dev/null
            ;;
        get|delete)
            local route="${2:-}"
            if [[ -z "$route" ]]; then
                usage
                exit 2
            fi
            curl_json "${action^^}" "$(join_url "$base_url" "$route")"
            ;;
        post|put|patch)
            local route="${2:-}"
            local payload="${3:-{}}"
            if [[ -z "$route" ]]; then
                usage
                exit 2
            fi

            local url
            url="$(join_url "$base_url" "$route")"
            if [[ "$payload" == @* ]]; then
                local payload_file="${payload#@}"
                if [[ ! -f "$payload_file" ]]; then
                    echo "[ashare-api] payload 文件不存在: $payload_file" >&2
                    exit 2
                fi
                curl_json "${action^^}" "$url" \
                    -H "Content-Type: application/json" \
                    --data-binary "@${payload_file}"
                return 0
            fi

            if [[ -f "$payload" ]]; then
                curl_json "${action^^}" "$url" \
                    -H "Content-Type: application/json" \
                    --data-binary "@${payload}"
                return 0
            fi

            curl_json "${action^^}" "$url" \
                -H "Content-Type: application/json" \
                --data-binary "$payload"
            ;;
        *)
            usage
            exit 2
            ;;
    esac
}

main "$@"
