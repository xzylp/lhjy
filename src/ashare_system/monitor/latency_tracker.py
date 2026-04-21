"""盘中关键链路延迟跟踪。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..infra.audit_store import StateStore


def record_latency_sample(
    state_store: StateStore | None,
    *,
    chain: str,
    stage: str,
    elapsed_ms: float,
    threshold_ms: float,
    trade_date: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sample = {
        "chain": str(chain or "").strip(),
        "stage": str(stage or "").strip(),
        "elapsed_ms": round(max(float(elapsed_ms or 0.0), 0.0), 3),
        "threshold_ms": round(max(float(threshold_ms or 0.0), 0.0), 3),
        "trade_date": str(trade_date or "").strip(),
        "recorded_at": datetime.now().isoformat(),
        "status": "ok",
        "metadata": dict(metadata or {}),
    }
    if sample["threshold_ms"] > 0 and sample["elapsed_ms"] > sample["threshold_ms"]:
        sample["status"] = "alert"
        sample["message"] = (
            f"{sample['chain']} 延迟 {sample['elapsed_ms']:.1f}ms，超过阈值 {sample['threshold_ms']:.1f}ms"
        )
    else:
        sample["message"] = (
            f"{sample['chain']} 延迟 {sample['elapsed_ms']:.1f}ms，阈值 {sample['threshold_ms']:.1f}ms"
        )
    if not state_store:
        return sample
    payload = dict(state_store.get("latency_tracker", {}) or {})
    latest = dict(payload.get("latest") or {})
    latest[sample["chain"]] = sample
    history = [dict(item) for item in list(payload.get("history") or []) if isinstance(item, dict)]
    history.append(sample)
    alerts = [dict(item) for item in list(payload.get("alerts") or []) if isinstance(item, dict)]
    if sample["status"] == "alert":
        alerts.append(sample)
    state_store.set(
        "latency_tracker",
        {
            "updated_at": sample["recorded_at"],
            "latest": latest,
            "history": history[-400:],
            "alerts": alerts[-100:],
        },
    )
    return sample


def get_latency_tracker_snapshot(state_store: StateStore | None) -> dict[str, Any]:
    if not state_store:
        return {"available": False, "latest": {}, "alerts": [], "history": []}
    payload = dict(state_store.get("latency_tracker", {}) or {})
    latest = dict(payload.get("latest") or {})
    alerts = [dict(item) for item in list(payload.get("alerts") or []) if isinstance(item, dict)]
    history = [dict(item) for item in list(payload.get("history") or []) if isinstance(item, dict)]
    return {
        "available": bool(latest or alerts),
        "updated_at": payload.get("updated_at"),
        "latest": latest,
        "alerts": alerts[-10:],
        "history": history[-20:],
        "summary_lines": [
            f"{key}: {float(item.get('elapsed_ms', 0.0) or 0.0):.1f}ms / {float(item.get('threshold_ms', 0.0) or 0.0):.1f}ms"
            for key, item in latest.items()
        ],
    }
