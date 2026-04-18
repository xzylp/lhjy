"""Linux 本地 Go 并发数据平台客户端"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..settings import AppSettings

logger = logging.getLogger(__name__)


def _sanitize_gateway_detail(detail: str) -> str:
    text = str(detail or "").strip()
    if not text:
        return text
    if "http://127.0.0.1:18792" in text or "127.0.0.1:18792" in text:
        return (
            "Windows 18791 网关已收到请求，但其内部交易桥 127.0.0.1:18792 当前不可用；"
            f"原始错误: {text}"
        )
    return text


class GoPlatformClient:
    def __init__(self, settings: AppSettings) -> None:
        self.base_url = str(settings.go_platform.base_url).rstrip("/")
        self.timeout = float(settings.go_platform.timeout_sec)
        self.connect_timeout = min(self.timeout, 3.0)
        self.client = httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            trust_env=False,
        )

    @staticmethod
    def _classify_http_status(status_code: int) -> str:
        if status_code == 429:
            return "go_platform_overloaded"
        if status_code == 401:
            return "go_platform_auth_failed"
        if status_code == 404:
            return "go_platform_not_found"
        if status_code == 408:
            return "go_platform_timeout"
        if 500 <= status_code <= 599:
            return "go_platform_upstream_error"
        if 400 <= status_code <= 499:
            return "go_platform_client_error"
        return "go_platform_http_error"

    @staticmethod
    def _error_message(prefix: str, path: str, detail: str | None = None) -> str:
        detail_text = _sanitize_gateway_detail(str(detail or "").strip())
        if detail_text:
            return f"{prefix}: {path} | {detail_text}"
        return f"{prefix}: {path}"

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        headers: dict | None = None,
        json: dict | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        req_headers = headers or {}
        if "X-Ashare-Priority" not in req_headers:
            if "/trade/" in path:
                req_headers["X-Ashare-Priority"] = "low"
            elif "/account/" in path:
                req_headers["X-Ashare-Priority"] = "medium"
            else:
                req_headers["X-Ashare-Priority"] = "high"

        url = f"{self.base_url}{path}"
        start_time = time.perf_counter()

        try:
            response = self.client.request(
                method,
                url,
                params=params,
                headers=req_headers,
                json=json,
                timeout=httpx.Timeout(timeout or self.timeout, connect=min(timeout or self.timeout, self.connect_timeout)),
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "go_platform_request | %s %s | status=%s | elapsed=%.2fms | priority=%s",
                method,
                path,
                response.status_code,
                elapsed_ms,
                req_headers.get("X-Ashare-Priority"),
            )
            if response.status_code >= 400:
                prefix = self._classify_http_status(response.status_code)
                detail = ""
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        detail = str(
                            payload.get("last_error")
                            or payload.get("message")
                            or payload.get("error")
                            or payload.get("detail")
                            or ""
                        )
                except Exception:
                    detail = response.text[:200]
                raise RuntimeError(self._error_message(prefix, path, detail))
            return response
        except httpx.TimeoutException as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "go_platform_timeout | %s %s | elapsed=%.2fms | error=%s",
                method,
                path,
                elapsed_ms,
                exc,
            )
            raise RuntimeError(self._error_message("go_platform_timeout", path, f"elapsed {elapsed_ms:.0f}ms")) from exc
        except httpx.ConnectError as exc:
            logger.error("go_platform_connect_error | %s %s | error=%s", method, path, exc)
            raise RuntimeError(self._error_message("go_platform_unavailable", path, str(exc))) from exc
        except Exception as exc:
            logger.error("go_platform_error | %s %s | error=%s", method, path, exc)
            raise

    def get_json(
        self,
        path: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            resp = self.request("GET", path, params=params, headers=headers, timeout=timeout)
            payload = resp.json()
            if not isinstance(payload, dict):
                raise RuntimeError(self._error_message("go_platform_invalid_payload", path))
            if payload.get("ok", True) is False:
                error = (
                    payload.get("last_error")
                    or payload.get("message")
                    or payload.get("error")
                    or payload.get("detail")
                    or "go_platform_request_failed"
                )
                error_text = str(error)
                if "invalid token" in error_text.lower():
                    raise RuntimeError(self._error_message("go_platform_auth_failed", path, error_text))
                raise RuntimeError(self._error_message("go_platform_request_failed", path, error_text))
            return payload
        except Exception as exc:
            if not isinstance(exc, RuntimeError) or "go_platform_" not in str(exc):
                logger.error("go_platform_json_parse_error | GET %s | error=%s", path, exc)
            raise
