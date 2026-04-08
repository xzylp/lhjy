#!/usr/bin/env bash
set -euo pipefail

if [[ "${ASHARE_OPS_PROXY_CLEAN:-0}" != "1" ]]; then
  exec env \
    -u http_proxy \
    -u https_proxy \
    -u HTTP_PROXY \
    -u HTTPS_PROXY \
    -u all_proxy \
    -u ALL_PROXY \
    -u no_proxy \
    -u NO_PROXY \
    ASHARE_OPS_PROXY_CLEAN=1 \
    "$0" "$@"
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPS_PORT="${ASHARE_OPS_PROXY_PORT:-18791}"
OPS_MANIFEST_PATH="${ASHARE_OPS_PROXY_MANIFEST_PATH:-$ROOT_DIR/.ashare_state/ops_proxy_endpoints.json}"
OPS_TOKEN_FILE="${ASHARE_OPS_PROXY_TOKEN_FILE:-$ROOT_DIR/.ashare_state/ops_proxy_token.txt}"

usage() {
  cat >&2 <<'EOF'
Usage:
  windows_ops_api.sh probe
  windows_ops_api.sh base-url
  windows_ops_api.sh GET /health
  windows_ops_api.sh POST /actions/service '{"action":"status"}'

Environment:
  ASHARE_WSL_OPS_URL       Optional explicit base URL for WSL -> Windows ops proxy
  ASHARE_OPS_PROXY_PORT    Optional port override, default 18791
  ASHARE_OPS_PROXY_TOKEN   Optional explicit token override
EOF
  exit 2
}

resolve_windows_host() {
  awk '/^nameserver[[:space:]]+/ { print $2; exit }' /etc/resolv.conf 2>/dev/null || true
}

resolve_default_gateway() {
  awk '
    $2 == "00000000" && $3 != "00000000" {
      hex = $3
      printf "%d.%d.%d.%d\n", \
        strtonum("0x" substr(hex,7,2)), \
        strtonum("0x" substr(hex,5,2)), \
        strtonum("0x" substr(hex,3,2)), \
        strtonum("0x" substr(hex,1,2))
      exit
    }
  ' /proc/net/route 2>/dev/null || true
}

resolve_manifest_candidates() {
  [[ -f "$OPS_MANIFEST_PATH" ]] || return 0
  python3 - "$OPS_MANIFEST_PATH" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(0)

preferred = data.get("preferred_wsl_url")
if isinstance(preferred, str) and preferred.strip():
    print(preferred.strip())

for item in data.get("candidate_urls", []):
    if isinstance(item, str) and item.strip():
        print(item.strip())
PY
}

resolve_token() {
  if [[ -n "${ASHARE_OPS_PROXY_TOKEN:-}" ]]; then
    printf '%s\n' "$ASHARE_OPS_PROXY_TOKEN"
    return 0
  fi
  if [[ -f "$OPS_TOKEN_FILE" ]]; then
    head -n 1 "$OPS_TOKEN_FILE" | tr -d '\r\n'
    return 0
  fi
  if [[ -f "$OPS_MANIFEST_PATH" ]]; then
    local manifest_token_file
    manifest_token_file="$(python3 - "$OPS_MANIFEST_PATH" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(0)

token_file = data.get("token_file")
if isinstance(token_file, str) and token_file.strip():
    print(token_file.strip())
PY
)"
    if [[ -n "$manifest_token_file" && -f "$manifest_token_file" ]]; then
      head -n 1 "$manifest_token_file" | tr -d '\r\n'
      return 0
    fi
  fi
  return 1
}

probe_url() {
  local url="$1"
  [[ "$url" =~ ^https?://[^/]+$ ]] || return 1
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY \
    curl --noproxy "*" -fsS --max-time 2 "${url}/health" >/dev/null 2>&1
}

resolve_base_url() {
  local -a candidates=()

  while IFS= read -r manifest_candidate; do
    [[ -n "$manifest_candidate" ]] || continue
    candidates+=("$manifest_candidate")
  done < <(resolve_manifest_candidates)

  if [[ -n "${ASHARE_WSL_OPS_URL:-}" ]]; then
    candidates+=("$ASHARE_WSL_OPS_URL")
  fi

  local localhost_url="http://127.0.0.1:${OPS_PORT}"
  candidates+=("$localhost_url")

  local windows_host
  windows_host="$(resolve_windows_host)"
  if [[ -n "$windows_host" ]]; then
    candidates+=("http://${windows_host}:${OPS_PORT}")
  fi

  local default_gateway
  default_gateway="$(resolve_default_gateway)"
  if [[ -n "$default_gateway" ]]; then
    candidates+=("http://${default_gateway}:${OPS_PORT}")
  fi

  local seen="|"
  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" =~ ^https?://[^/]+$ ]] || continue
    if [[ "$seen" == *"|${candidate}|"* ]]; then
      continue
    fi
    seen="${seen}${candidate}|"
    if probe_url "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  cat >&2 <<EOF
Unable to reach Windows ops proxy from WSL.
Checked:
  - ${localhost_url}
  - http://${windows_host:-<unresolved>}:${OPS_PORT}
  - http://${default_gateway:-<unresolved>}:${OPS_PORT}

Expected:
  1. Windows ops proxy is running
  2. Manifest is current: ${OPS_MANIFEST_PATH}
EOF
  exit 1
}

build_url() {
  local path="$1"
  if [[ "$path" =~ ^https?:// ]]; then
    printf '%s\n' "$path"
    return 0
  fi

  local base_url
  base_url="$(resolve_base_url)"
  if [[ "$path" != /* ]]; then
    path="/${path}"
  fi
  printf '%s%s\n' "$base_url" "$path"
}

request() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local token
  token="$(resolve_token || true)"
  local url
  url="$(build_url "$path")"
  local -a curl_args=(
    --noproxy "*" -fsS -X "$method" "$url"
  )
  if [[ -n "$token" ]]; then
    curl_args+=(-H "X-Ashare-Token: $token")
  fi
  if [[ -n "$body" ]]; then
    curl_args+=(-H "Content-Type: application/json" --data "$body")
  fi
  exec env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY \
    curl "${curl_args[@]}"
}

command_name="${1:-}"
command_upper="$(printf '%s' "$command_name" | tr '[:lower:]' '[:upper:]')"

case "$command_upper" in
  PROBE)
    base_url="$(resolve_base_url)"
    printf 'ASHARE_WSL_OPS_URL=%s\n' "$base_url"
    ;;
  BASE-URL)
    resolve_base_url
    ;;
  GET|POST|PUT|PATCH|DELETE)
    method="$command_upper"
    path="${2:-}"
    body="${3:-}"
    [[ -n "$path" ]] || usage
    request "$method" "$path" "$body"
    ;;
  *)
    usage
    ;;
esac
