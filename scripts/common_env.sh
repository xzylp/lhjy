#!/usr/bin/env bash
# ashare-system-v2 运行脚本公共环境解析
set -euo pipefail

resolve_project_python() {
    if [[ -n "${ASHARE_PYTHON_BIN:-}" && -x "${ASHARE_PYTHON_BIN}" ]]; then
        printf '%s\n' "${ASHARE_PYTHON_BIN}"
        return 0
    fi

    if [[ -x ".venv/bin/python" ]]; then
        printf '%s\n' ".venv/bin/python"
        return 0
    fi

    if [[ -f ".venv/Scripts/python.exe" ]]; then
        printf '%s\n' ".venv/Scripts/python.exe"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        printf '%s\n' "$(command -v python3)"
        return 0
    fi

    if command -v python >/dev/null 2>&1; then
        printf '%s\n' "$(command -v python)"
        return 0
    fi

    return 1
}

resolve_openclaw_bin() {
    if [[ -n "${OPENCLAW_BIN:-}" && -x "${OPENCLAW_BIN}" ]]; then
        printf '%s\n' "${OPENCLAW_BIN}"
        return 0
    fi

    if command -v openclaw >/dev/null 2>&1; then
        printf '%s\n' "$(command -v openclaw)"
        return 0
    fi

    if [[ -x "/home/yxz/.npm-global/bin/openclaw" ]]; then
        printf '%s\n' "/home/yxz/.npm-global/bin/openclaw"
        return 0
    fi

    return 1
}

rotate_log_file_if_needed() {
    local log_file="${1:-}"
    local max_mb="${2:-50}"

    if [[ -z "${log_file}" ]]; then
        return 0
    fi

    mkdir -p "$(dirname "${log_file}")"
    if [[ ! -f "${log_file}" ]]; then
        return 0
    fi

    local size_bytes
    size_bytes="$(wc -c < "${log_file}" | tr -d '[:space:]')"
    local max_bytes=$(( max_mb * 1024 * 1024 ))
    if [[ "${size_bytes}" -lt "${max_bytes}" ]]; then
        return 0
    fi

    local timestamp
    timestamp="$(date +%Y%m%d_%H%M%S)"
    local rotated="${log_file}.${timestamp}"
    mv "${log_file}" "${rotated}"
    : > "${log_file}"
    printf '[ashare-v2] rotated log %s -> %s (size=%s bytes)\n' "${log_file}" "${rotated}" "${size_bytes}"
}
