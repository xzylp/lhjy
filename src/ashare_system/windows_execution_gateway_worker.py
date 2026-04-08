"""Windows Execution Gateway worker。

最小职责：
- 从 Linux 控制面拉取待执行 intent
- 认领 intent
- 调用注入的执行器提交订单
- 回写 receipt

默认不内置真实 QMT 适配逻辑，避免在未明确配置时误触 live。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import httpx

from .contracts import ExecutionGatewayClaimInput, ExecutionGatewayReceiptInput, ExecutionIntentPacket, PlaceOrderRequest
from .infra.adapters import ExecutionAdapter, XtQuantExecutionAdapter
from .settings import load_settings


ExecutionSubmitter = Callable[[dict[str, Any]], dict[str, Any]]

_STATUS_ALIASES = {
    "accepted": "submitted",
    "partial-filled": "partial_filled",
    "partial_filled": "partial_filled",
    "cancelled": "canceled",
}
_ALLOWED_RECEIPT_STATUSES = {
    "submitted",
    "partial_filled",
    "filled",
    "canceled",
    "rejected",
    "failed",
}


@dataclass(slots=True)
class GatewayWorkerConfig:
    control_plane_base_url: str
    source_id: str
    deployment_role: str = ""
    bridge_path: str = ""
    account_id: str = ""
    poll_limit: int = 20
    timeout_sec: float = 10.0
    poll_interval_sec: float = 3.0
    pending_path: str = "/system/execution/gateway/intents/pending"
    claim_path: str = "/system/execution/gateway/intents/claim"
    receipt_path: str = "/system/execution/gateway/receipts"


class ExecutionGatewayWorker:
    def __init__(
        self,
        config: GatewayWorkerConfig,
        *,
        submitter: ExecutionSubmitter | None = None,
        client: httpx.Client | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.submitter = submitter or self._default_submitter
        self.now_factory = now_factory or datetime.now
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=config.timeout_sec)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def poll_pending_intents(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "gateway_source_id": self.config.source_id,
            "deployment_role": self.config.deployment_role,
            "limit": self.config.poll_limit,
        }
        if self.config.account_id:
            params["account_id"] = self.config.account_id
        payload = self._get_json(self.config.pending_path, params=params)
        if not payload.get("ok", False):
            raise RuntimeError(f"poll pending intents failed: {payload}")
        items = payload.get("items") or []
        return [ExecutionIntentPacket.model_validate(item).model_dump() for item in items]

    def claim_intent(self, intent_id: str) -> dict[str, Any]:
        claim = ExecutionGatewayClaimInput(
            intent_id=intent_id,
            gateway_source_id=self.config.source_id,
            deployment_role=self.config.deployment_role,
            bridge_path=self.config.bridge_path,
            claimed_at=self.now_factory().isoformat(),
        )
        return self._post_json(self.config.claim_path, claim.model_dump())

    def post_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        payload = ExecutionGatewayReceiptInput.model_validate(receipt).model_dump()
        return self._post_json(self.config.receipt_path, payload)

    def process_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        claimed = self.claim_intent(str(intent.get("intent_id") or ""))
        claim_status = str(claimed.get("claim_status") or "")
        if not claimed.get("ok", False) or claim_status != "claimed":
            return {
                "intent_id": intent.get("intent_id"),
                "phase": "claim",
                "ok": False,
                "claim_status": claim_status or "failed",
                "reason": claimed.get("reason") or "claim_failed",
            }

        claimed_intent = claimed.get("intent") or intent
        submit_error = ""
        submit_ok = True
        try:
            submit_result = self.submitter(claimed_intent)
            receipt = self._build_receipt(claimed_intent, submit_result)
        except Exception as exc:
            submit_ok = False
            submit_error = str(exc)
            receipt = self._build_failed_receipt(claimed_intent, str(exc))
        try:
            receipt_response = self.post_receipt(receipt)
        except Exception as exc:
            return {
                "intent_id": claimed_intent.get("intent_id"),
                "phase": "receipt",
                "ok": False,
                "claim_status": claim_status,
                "submit_ok": submit_ok,
                "submit_error": submit_error,
                "receipt_status": receipt["status"],
                "stored": False,
                "reason": str(exc),
                "latest_receipt": {},
            }
        result_phase = "receipt" if submit_ok else "submit"
        return {
            "intent_id": claimed_intent.get("intent_id"),
            "phase": result_phase,
            "ok": bool(receipt_response.get("ok", False)),
            "claim_status": claim_status,
            "submit_ok": submit_ok,
            "submit_error": submit_error,
            "receipt_status": receipt["status"],
            "stored": bool(receipt_response.get("stored", False)),
            "reason": receipt_response.get("reason") or "",
            "latest_receipt": receipt_response.get("latest_receipt") or {},
        }

    def run_once(self) -> dict[str, Any]:
        try:
            intents = self.poll_pending_intents()
        except Exception as exc:
            reason = f"poll pending intents failed: {exc}"
            summary_lines = [f"execution gateway worker run failed at poll: {exc}"]
            return {
                "ok": False,
                "source_id": self.config.source_id,
                "deployment_role": self.config.deployment_role,
                "bridge_path": self.config.bridge_path,
                "polled_count": 0,
                "claimed_count": 0,
                "stored_count": 0,
                "failed_count": 1,
                "results": [
                    {
                        "phase": "poll",
                        "ok": False,
                        "reason": reason,
                    }
                ],
                "summary_lines": summary_lines,
                "processed_at": self.now_factory().isoformat(),
            }
        results: list[dict[str, Any]] = []
        claimed_count = 0
        stored_count = 0
        failed_count = 0
        for intent in intents:
            result = self.process_intent(intent)
            results.append(result)
            if result.get("claim_status") == "claimed":
                claimed_count += 1
            if result.get("ok"):
                stored_count += 1
            else:
                failed_count += 1
        summary_lines = [
            (
                "execution gateway worker run complete: "
                f"polled={len(intents)} claimed={claimed_count} stored={stored_count} failed={failed_count}."
            )
        ]
        if failed_count:
            failed_phases = [str(item.get("phase") or "unknown") for item in results if not item.get("ok")]
            summary_lines.append("failed_phases=" + ",".join(failed_phases))
        return {
            "ok": failed_count == 0,
            "source_id": self.config.source_id,
            "deployment_role": self.config.deployment_role,
            "bridge_path": self.config.bridge_path,
            "polled_count": len(intents),
            "claimed_count": claimed_count,
            "stored_count": stored_count,
            "failed_count": failed_count,
            "results": results,
            "summary_lines": summary_lines,
            "processed_at": self.now_factory().isoformat(),
        }

    def run_forever(self) -> None:
        while True:
            payload = self.run_once()
            print(json.dumps(payload, ensure_ascii=False))
            time.sleep(max(self.config.poll_interval_sec, 0.2))

    def _build_receipt(self, intent: dict[str, Any], submit_result: dict[str, Any]) -> dict[str, Any]:
        reported_at = self.now_factory().isoformat()
        status = self._normalize_receipt_status(submit_result.get("status"))
        summary_lines = list(submit_result.get("summary_lines") or [])
        if not summary_lines:
            summary_lines = [f"intent {intent.get('intent_id', '')} 已由 Windows Gateway 提交。"]
        receipt = ExecutionGatewayReceiptInput(
            receipt_id=str(
                submit_result.get("receipt_id")
                or f"{intent.get('intent_id', 'intent')}-{status}-{reported_at.replace(':', '').replace('-', '')}"
            ),
            intent_id=str(intent.get("intent_id") or ""),
            intent_version=str(intent.get("intent_version") or "v1"),
            gateway_source_id=self.config.source_id,
            deployment_role=self.config.deployment_role,
            bridge_path=self.config.bridge_path,
            reported_at=reported_at,
            submitted_at=str(submit_result.get("submitted_at") or reported_at),
            status=status,
            broker_order_id=str(submit_result.get("broker_order_id") or ""),
            broker_session_id=str(submit_result.get("broker_session_id") or ""),
            exchange_order_id=str(submit_result.get("exchange_order_id") or ""),
            error_code=str(submit_result.get("error_code") or ""),
            error_message=str(submit_result.get("error_message") or ""),
            order=dict(submit_result.get("order") or {}),
            fills=list(submit_result.get("fills") or []),
            latency_ms=submit_result.get("latency_ms"),
            raw_payload=dict(submit_result.get("raw_payload") or {}),
            summary_lines=summary_lines,
        )
        return receipt.model_dump()

    def _build_failed_receipt(self, intent: dict[str, Any], error_message: str) -> dict[str, Any]:
        reported_at = self.now_factory().isoformat()
        receipt = ExecutionGatewayReceiptInput(
            receipt_id=f"{intent.get('intent_id', 'intent')}-failed-{reported_at.replace(':', '').replace('-', '')}",
            intent_id=str(intent.get("intent_id") or ""),
            intent_version=str(intent.get("intent_version") or "v1"),
            gateway_source_id=self.config.source_id,
            deployment_role=self.config.deployment_role,
            bridge_path=self.config.bridge_path,
            reported_at=reported_at,
            submitted_at="",
            status="failed",
            error_code="gateway_submit_failed",
            error_message=error_message,
            summary_lines=[f"intent {intent.get('intent_id', '')} 提交失败: {error_message}"],
        )
        return receipt.model_dump()

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.get(self._url(path), params=params)
        response.raise_for_status()
        return dict(response.json())

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(self._url(path), json=payload)
        response.raise_for_status()
        return dict(response.json())

    def _url(self, path: str) -> str:
        return f"{self.config.control_plane_base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _normalize_receipt_status(value: Any) -> str:
        status = str(value or "submitted").strip().lower().replace(" ", "_")
        status = _STATUS_ALIASES.get(status, status)
        if status not in _ALLOWED_RECEIPT_STATUSES:
            raise ValueError(f"unsupported receipt status: {value}")
        return status

    @staticmethod
    def _default_submitter(_intent: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("execution submitter not configured")


def build_adapter_submitter(adapter: ExecutionAdapter) -> ExecutionSubmitter:
    def _submit_with_adapter(intent: dict[str, Any]) -> dict[str, Any]:
        request_payload = dict(intent.get("request") or {})
        request = PlaceOrderRequest.model_validate(request_payload)
        order = adapter.place_order(request)
        adapter_mode = str(getattr(adapter, "mode", "") or adapter.__class__.__name__)
        return {
            "status": "submitted",
            "broker_order_id": str(order.order_id),
            "submitted_at": datetime.now().isoformat(),
            "order": order.model_dump(),
            "raw_payload": {
                "adapter_mode": adapter_mode,
                "request_id": request.request_id,
            },
            "summary_lines": [f"{adapter_mode} 已提交 intent {intent.get('intent_id', '')} 对应订单。"],
        }

    return _submit_with_adapter


def _build_submitter(executor_mode: str, *, adapter: ExecutionAdapter | None = None) -> ExecutionSubmitter:
    normalized_mode = str(executor_mode or "").strip().lower()
    if normalized_mode in {"", "fail_unconfigured"}:
        return ExecutionGatewayWorker._default_submitter
    if normalized_mode == "noop_success":
        def _noop_success(intent: dict[str, Any]) -> dict[str, Any]:
            request = dict(intent.get("request") or {})
            return {
                "status": "submitted",
                "broker_order_id": str(request.get("request_id") or intent.get("intent_id") or ""),
                "order": {
                    "order_id": str(request.get("request_id") or intent.get("intent_id") or ""),
                    "account_id": str(request.get("account_id") or intent.get("account_id") or ""),
                    "symbol": str(request.get("symbol") or intent.get("symbol") or ""),
                    "side": str(request.get("side") or intent.get("side") or "BUY"),
                    "quantity": int(request.get("quantity") or intent.get("quantity") or 0),
                    "price": float(request.get("price") or intent.get("price") or 0.0),
                    "status": "PENDING",
                },
                "summary_lines": ["noop_success 模式：未接真实 QMT，仅完成协议闭环演练。"],
            }

        return _noop_success
    if normalized_mode == "xtquant":
        resolved_adapter = adapter
        if resolved_adapter is None:
            settings = load_settings()
            resolved_adapter = XtQuantExecutionAdapter(settings)
            resolved_adapter.mode = "xtquant"
        return build_adapter_submitter(resolved_adapter)
    raise ValueError(f"unsupported executor mode: {executor_mode}")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Windows Execution Gateway worker")
    parser.add_argument("--control-plane-base-url", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--deployment-role", default="")
    parser.add_argument("--bridge-path", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--poll-limit", type=int, default=20)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument("--poll-interval-sec", type=float, default=3.0)
    parser.add_argument("--executor-mode", default="fail_unconfigured", choices=["fail_unconfigured", "noop_success", "xtquant"])
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    worker = ExecutionGatewayWorker(
        GatewayWorkerConfig(
            control_plane_base_url=args.control_plane_base_url,
            source_id=args.source_id,
            deployment_role=args.deployment_role,
            bridge_path=args.bridge_path,
            account_id=args.account_id,
            poll_limit=args.poll_limit,
            timeout_sec=args.timeout_sec,
            poll_interval_sec=args.poll_interval_sec,
        ),
        submitter=_build_submitter(args.executor_mode),
    )
    try:
        if args.once:
            payload = worker.run_once()
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if payload.get("ok") else 1
        worker.run_forever()
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
