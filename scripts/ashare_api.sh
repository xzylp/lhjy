#!/usr/bin/env bash
set -euo pipefail

if [[ "${ASHARE_API_PROXY_CLEAN:-0}" != "1" ]]; then
  exec env \
    -u http_proxy \
    -u https_proxy \
    -u HTTP_PROXY \
    -u HTTPS_PROXY \
    -u all_proxy \
    -u ALL_PROXY \
    -u no_proxy \
    -u NO_PROXY \
    ASHARE_API_PROXY_CLEAN=1 \
    "$0" "$@"
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_PORT="${ASHARE_SERVICE_PORT:-8100}"
SERVICE_MANIFEST_PATH="${ASHARE_SERVICE_MANIFEST_PATH:-$ROOT_DIR/.ashare_state/service_endpoints.json}"

usage() {
  cat >&2 <<'EOF'
Usage:
  ashare_api.sh probe
  ashare_api.sh base-url
  ashare_api.sh GET /health
  ashare_api.sh POST /runtime/jobs/pipeline '{"universe_scope":"a-share","max_candidates":8,"auto_trade":false,"account_id":"8890130545"}'

Environment:
  ASHARE_WSL_SERVICE_URL   Optional explicit base URL for WSL -> Windows FastAPI
  ASHARE_SERVICE_PORT      Optional port override, default 8100
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

resolve_windows_wsl_host_via_powershell() {
  command -v powershell.exe >/dev/null 2>&1 || return 0
  powershell.exe -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { \$_.InterfaceAlias -like 'vEthernet*' -or \$_.InterfaceAlias -like '*WSL*' } | Select-Object -ExpandProperty IPAddress -First 1" 2>/dev/null \
    | tr -d '\r' \
    | awk 'NF { print; exit }' || true
}

resolve_manifest_candidates() {
  [[ -f "$SERVICE_MANIFEST_PATH" ]] || return 0
  python3 - "$SERVICE_MANIFEST_PATH" <<'PY'
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

windows_local_request() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  command -v powershell.exe >/dev/null 2>&1 || return 1
  if [[ "$path" != /* ]]; then
    path="/${path}"
  fi

  if [[ -n "$body" ]]; then
    printf '%s' "$body" | powershell.exe -NoProfile -Command '
      $method = $args[0]
      $path = $args[1]
      $body = [Console]::In.ReadToEnd()
      $uri = "http://127.0.0.1:'"${SERVICE_PORT}"'" + $path
      $resp = Invoke-RestMethod -Uri $uri -Method $method -ContentType "application/json" -Body $body -ErrorAction Stop
      $resp | ConvertTo-Json -Depth 100 -Compress
    ' -- "$method" "$path" | tr -d '\r'
    return 0
  fi

  powershell.exe -NoProfile -Command '
    $method = $args[0]
    $path = $args[1]
    $uri = "http://127.0.0.1:'"${SERVICE_PORT}"'" + $path
    $resp = Invoke-RestMethod -Uri $uri -Method $method -ErrorAction Stop
    $resp | ConvertTo-Json -Depth 100 -Compress
  ' -- "$method" "$path" | tr -d '\r'
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

  if [[ -n "${ASHARE_WSL_SERVICE_URL:-}" ]]; then
    candidates+=("$ASHARE_WSL_SERVICE_URL")
  fi

  local localhost_url="http://127.0.0.1:${SERVICE_PORT}"
  candidates+=("$localhost_url")

  local windows_host
  windows_host="$(resolve_windows_host)"
  if [[ -n "$windows_host" ]]; then
    candidates+=("http://${windows_host}:${SERVICE_PORT}")
  fi

  local windows_wsl_host
  windows_wsl_host="$(resolve_windows_wsl_host_via_powershell)"
  if [[ -n "$windows_wsl_host" ]]; then
    candidates+=("http://${windows_wsl_host}:${SERVICE_PORT}")
  fi

  local default_gateway
  default_gateway="$(resolve_default_gateway)"
  if [[ -n "$default_gateway" ]]; then
    candidates+=("http://${default_gateway}:${SERVICE_PORT}")
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
Unable to reach ashare-system-v2 from WSL.
Checked:
  - ${localhost_url}
  - http://${windows_host:-<unresolved>}:${SERVICE_PORT}
  - http://${windows_wsl_host:-<unresolved>}:${SERVICE_PORT}
  - http://${default_gateway:-<unresolved>}:${SERVICE_PORT}

Expected:
  1. Windows service endpoint manifest is current: ${SERVICE_MANIFEST_PATH}
  2. Windows service is listening on an address reachable from WSL
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
  base_url="$(resolve_base_url)" || return 1
  if [[ "$path" != /* ]]; then
    path="/${path}"
  fi
  printf '%s%s\n' "$base_url" "$path"
}

command_name="${1:-}"
command_upper="$(printf '%s' "$command_name" | tr '[:lower:]' '[:upper:]')"

case "$command_upper" in
  PROBE)
    base_url="$(resolve_base_url)"
    printf 'ASHARE_WSL_SERVICE_URL=%s\n' "$base_url"
    ;;
  BASE-URL)
    resolve_base_url
    ;;
  GET|POST|PUT|PATCH|DELETE)
    method="$command_upper"
    path="${2:-}"
    body="${3:-}"
    [[ -n "$path" ]] || usage
    if ! build_output="$(build_url "$path" 2>&1)"; then
      if windows_local_request "$method" "$path" "$body"; then
        exit 0
      fi
      printf '%s\n' "$build_output" >&2
      exit 1
    fi
    url="$build_output"
    if [[ -n "$body" ]]; then
      exec env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY \
        curl --noproxy "*" -fsS -X "$method" "$url" -H "Content-Type: application/json" --data "$body"
    fi
    exec env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY \
      curl --noproxy "*" -fsS -X "$method" "$url"
    ;;
  *)
    usage
    ;;
esac
