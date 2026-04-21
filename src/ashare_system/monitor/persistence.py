"""盯盘状态持久化。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from ..contracts import QuoteSnapshot
from ..data.archive import DataArchiveStore
from ..infra.audit_store import StateStore
from ..logging_config import get_logger
from ..runtime_config import RuntimeConfig, RuntimeConfigManager
from .alert_engine import AlertEvent

logger = get_logger("monitor.persistence")

EXECUTION_HEALTH_STATUS_SCORES = {
    "down": 0,
    "unknown": 1,
    "degraded": 2,
    "healthy": 3,
}


def _empty_exit_snapshot_payload() -> dict:
    return {
        "version": "v1",
        "checked_at": 0.0,
        "signal_count": 0,
        "watched_symbols": [],
        "by_symbol": [],
        "by_reason": [],
        "by_severity": [],
        "by_tag": [],
        "summary_lines": ["当前无退出监控信号。"],
        "items": [],
    }


def _empty_execution_bridge_health_payload() -> dict:
    return {
        "version": "v1",
        "checked_at": 0.0,
        "reported_at": "",
        "source_id": "",
        "deployment_role": "",
        "bridge_path": "",
        "overall_status": "unknown",
        "gateway_online": False,
        "qmt_connected": False,
        "account_id": "",
        "session_fresh_seconds": 0,
        "attention_components": [],
        "attention_component_keys": [],
        "last_poll_at": "",
        "last_receipt_at": "",
        "last_error": "",
        "windows_execution_gateway": {
            "key": "windows_execution_gateway",
            "label": "Windows Execution Gateway",
            "status": "unknown",
            "reachable": False,
            "latency_ms": 0.0,
            "staleness_seconds": 0.0,
            "error_count": 0,
            "success_count": 0,
            "last_ok_at": "",
            "last_error_at": "",
            "detail": "",
            "tags": [],
        },
        "qmt_vm": {
            "key": "qmt_vm",
            "label": "QMT VM",
            "status": "unknown",
            "reachable": False,
            "latency_ms": 0.0,
            "staleness_seconds": 0.0,
            "error_count": 0,
            "success_count": 0,
            "last_ok_at": "",
            "last_error_at": "",
            "detail": "",
            "tags": [],
        },
        "component_health": [
            {
                "key": "windows_execution_gateway",
                "label": "Windows Execution Gateway",
                "status": "unknown",
                "reachable": False,
                "latency_ms": 0.0,
                "staleness_seconds": 0.0,
                "error_count": 0,
                "detail": "",
                "tags": [],
            },
            {
                "key": "qmt_vm",
                "label": "QMT VM",
                "status": "unknown",
                "reachable": False,
                "latency_ms": 0.0,
                "staleness_seconds": 0.0,
                "error_count": 0,
                "detail": "",
                "tags": [],
            },
        ],
        "summary_lines": ["Windows Execution Gateway 健康快照缺失。"],
        "updated_at": "",
    }


def build_execution_bridge_health_ingress_payload(
    health: dict | None = None,
    *,
    trigger: str = "windows_gateway",
    reported_at: str = "",
    source_id: str = "",
    deployment_role: str = "",
    bridge_path: str = "",
) -> dict:
    """构造 execution bridge 健康上报 payload（POST /system/monitor/execution-bridge-health）。"""

    raw_health = dict(health or {})
    payload = _empty_execution_bridge_health_payload()
    payload["version"] = str(raw_health.get("version") or payload["version"])
    payload["checked_at"] = float(raw_health.get("checked_at", payload["checked_at"]) or 0.0)
    payload["reported_at"] = str(
        raw_health.get("reported_at")
        or reported_at
        or raw_health.get("updated_at")
        or raw_health.get("last_poll_at")
        or payload["reported_at"]
    )
    payload["source_id"] = str(raw_health.get("source_id") or source_id or payload["source_id"])
    payload["deployment_role"] = str(raw_health.get("deployment_role") or deployment_role or payload["deployment_role"])
    payload["bridge_path"] = str(raw_health.get("bridge_path") or bridge_path or payload["bridge_path"])
    payload["overall_status"] = str(raw_health.get("overall_status") or payload["overall_status"])
    payload["gateway_online"] = bool(raw_health.get("gateway_online", payload["gateway_online"]))
    payload["qmt_connected"] = bool(raw_health.get("qmt_connected", payload["qmt_connected"]))
    payload["account_id"] = str(raw_health.get("account_id") or payload["account_id"])
    payload["session_fresh_seconds"] = int(raw_health.get("session_fresh_seconds", payload["session_fresh_seconds"]) or 0)
    payload["attention_components"] = [str(item) for item in list(raw_health.get("attention_components", [])) if str(item)]
    payload["attention_component_keys"] = [str(item) for item in list(raw_health.get("attention_component_keys", [])) if str(item)]
    payload["last_poll_at"] = str(raw_health.get("last_poll_at") or payload["last_poll_at"])
    payload["last_receipt_at"] = str(raw_health.get("last_receipt_at") or payload["last_receipt_at"])
    payload["last_error"] = str(raw_health.get("last_error") or payload["last_error"])
    payload["summary_lines"] = list(raw_health.get("summary_lines", payload["summary_lines"]))
    payload["updated_at"] = str(raw_health.get("updated_at") or payload["updated_at"])

    windows_execution_gateway = dict(raw_health.get("windows_execution_gateway") or {})
    qmt_vm = dict(raw_health.get("qmt_vm") or {})
    payload["windows_execution_gateway"] = {
        **dict(payload["windows_execution_gateway"]),
        **windows_execution_gateway,
    }
    payload["qmt_vm"] = {
        **dict(payload["qmt_vm"]),
        **qmt_vm,
    }
    if "reachable" not in windows_execution_gateway:
        payload["windows_execution_gateway"]["reachable"] = bool(payload["gateway_online"])
    if "reachable" not in qmt_vm:
        payload["qmt_vm"]["reachable"] = bool(payload["qmt_connected"])
    payload["component_health"] = [
        {
            "key": str(payload["windows_execution_gateway"].get("key") or "windows_execution_gateway"),
            "label": str(payload["windows_execution_gateway"].get("label") or "Windows Execution Gateway"),
            "status": str(payload["windows_execution_gateway"].get("status") or "unknown"),
            "reachable": bool(payload["windows_execution_gateway"].get("reachable", False)),
            "latency_ms": float(payload["windows_execution_gateway"].get("latency_ms", 0.0) or 0.0),
            "staleness_seconds": float(payload["windows_execution_gateway"].get("staleness_seconds", 0.0) or 0.0),
            "error_count": int(payload["windows_execution_gateway"].get("error_count", 0) or 0),
            "detail": str(payload["windows_execution_gateway"].get("detail") or ""),
            "tags": [str(item) for item in list(payload["windows_execution_gateway"].get("tags", [])) if str(item)],
        },
        {
            "key": str(payload["qmt_vm"].get("key") or "qmt_vm"),
            "label": str(payload["qmt_vm"].get("label") or "QMT VM"),
            "status": str(payload["qmt_vm"].get("status") or "unknown"),
            "reachable": bool(payload["qmt_vm"].get("reachable", False)),
            "latency_ms": float(payload["qmt_vm"].get("latency_ms", 0.0) or 0.0),
            "staleness_seconds": float(payload["qmt_vm"].get("staleness_seconds", 0.0) or 0.0),
            "error_count": int(payload["qmt_vm"].get("error_count", 0) or 0),
            "detail": str(payload["qmt_vm"].get("detail") or ""),
            "tags": [str(item) for item in list(payload["qmt_vm"].get("tags", [])) if str(item)],
        },
    ]
    if not payload["attention_component_keys"] and payload["attention_components"]:
        labels, keys = MonitorStateService._resolve_execution_bridge_attention_components(
            payload["attention_components"],
            payload["windows_execution_gateway"],
            payload["qmt_vm"],
        )
        payload["attention_components"] = labels
        payload["attention_component_keys"] = keys
    return {
        "trigger": str(trigger or "windows_gateway"),
        "health": payload,
    }


def build_execution_bridge_health_client_template(
    *,
    trigger: str = "windows_gateway",
    source_id: str = "windows-vm-a",
    deployment_role: str = "primary_gateway",
    bridge_path: str = "linux_openclaw -> windows_gateway -> qmt_vm",
) -> dict:
    """返回 Windows Execution Gateway 可直接抄用的 execution bridge health 上报模板。"""

    request_body = build_execution_bridge_health_ingress_payload(
        trigger=trigger,
        source_id=source_id,
        deployment_role=deployment_role,
        bridge_path=bridge_path,
        health={
            "reported_at": "",
            "gateway_online": False,
            "qmt_connected": False,
            "overall_status": "unknown",
        },
    )
    minimal_request_body = {
        "trigger": request_body["trigger"],
        "health": {
            "reported_at": request_body["health"]["reported_at"],
            "source_id": request_body["health"]["source_id"],
            "deployment_role": request_body["health"]["deployment_role"],
            "bridge_path": request_body["health"]["bridge_path"],
            "gateway_online": request_body["health"]["gateway_online"],
            "qmt_connected": request_body["health"]["qmt_connected"],
        },
    }
    return {
        "method": "POST",
        "path": "/system/monitor/execution-bridge-health",
        "content_type": "application/json",
        "request_body": request_body,
        "minimal_request_body": minimal_request_body,
        "top_level_health_defaults": dict(_empty_execution_bridge_health_payload()),
        "latest_read_descriptor": get_execution_bridge_health_latest_descriptor(),
        "source_value_suggestions": {
            "linux_openclaw": {
                "source_id": "linux-openclaw-main",
                "deployment_role": "linux_control_plane",
            },
            "windows_gateway": {
                "source_id": source_id,
                "deployment_role": deployment_role,
                "bridge_path": bridge_path,
            },
            "source_id": {
                "linux_openclaw": "linux-openclaw-main",
                "windows_gateway": source_id,
            },
            "deployment_role": {
                "linux_openclaw": "linux_control_plane",
                "windows_gateway": deployment_role,
            },
            "bridge_path": {
                "primary": "linux_openclaw -> windows_gateway -> qmt_vm",
                "backup": "linux_openclaw -> windows_gateway_backup -> qmt_vm",
            },
        },
    }


def get_execution_bridge_health_latest_descriptor() -> dict:
    """返回 Linux 主控读取 execution bridge latest/trend 的推荐字段描述。"""

    return {
        "latest_execution_bridge_health": {
            "root_key": "latest_execution_bridge_health",
            "health_key": "latest_execution_bridge_health.health",
            "recommended_fields": {
                "reported_at": "latest_execution_bridge_health.health.reported_at",
                "source_id": "latest_execution_bridge_health.health.source_id",
                "deployment_role": "latest_execution_bridge_health.health.deployment_role",
                "bridge_path": "latest_execution_bridge_health.health.bridge_path",
                "overall_status": "latest_execution_bridge_health.health.overall_status",
                "gateway_status": "latest_execution_bridge_health.health.windows_execution_gateway.status",
                "qmt_vm_status": "latest_execution_bridge_health.health.qmt_vm.status",
                "summary_lines": "latest_execution_bridge_health.health.summary_lines",
            },
        },
        "execution_bridge_health_trend_summary": {
            "root_key": "execution_bridge_health_trend_summary",
            "recommended_fields": {
                "latest_reported_at": "execution_bridge_health_trend_summary.latest_reported_at",
                "latest_source_id": "execution_bridge_health_trend_summary.latest_source_id",
                "latest_deployment_role": "execution_bridge_health_trend_summary.latest_deployment_role",
                "latest_bridge_path": "execution_bridge_health_trend_summary.latest_bridge_path",
                "trend_status": "execution_bridge_health_trend_summary.trend_status",
                "latest_overall_status": "execution_bridge_health_trend_summary.latest_overall_status",
                "health_trend_snapshot": "execution_bridge_health_trend_summary.health_trend_snapshot",
                "summary_lines": "execution_bridge_health_trend_summary.summary_lines",
            },
        },
        "source_value_suggestions": {
            "linux_openclaw": {
                "source_id": "linux-openclaw-main",
                "deployment_role": "linux_control_plane",
            },
            "windows_gateway_primary": {
                "source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            },
            "windows_gateway_backup": {
                "source_id": "windows-vm-b",
                "deployment_role": "backup_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway_backup -> qmt_vm",
            },
        },
    }


def build_execution_bridge_health_deployment_contract_sample(
    *,
    api_base_url: str = "http://127.0.0.1:18793",
    trigger: str = "windows_gateway",
) -> dict:
    """返回 execution bridge 统一部署契约样本（Windows 上报 + Linux 读取）。"""

    primary_sample = {
        "source_id": "windows-vm-a",
        "deployment_role": "primary_gateway",
        "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
    }
    backup_sample = {
        "source_id": "windows-vm-b",
        "deployment_role": "backup_gateway",
        "bridge_path": "linux_openclaw -> windows_gateway_backup -> qmt_vm",
    }
    client_template = build_execution_bridge_health_client_template(
        trigger=trigger,
        source_id=primary_sample["source_id"],
        deployment_role=primary_sample["deployment_role"],
        bridge_path=primary_sample["bridge_path"],
    )
    backup_client_template = build_execution_bridge_health_client_template(
        trigger=trigger,
        source_id=backup_sample["source_id"],
        deployment_role=backup_sample["deployment_role"],
        bridge_path=backup_sample["bridge_path"],
    )
    latest_descriptor = get_execution_bridge_health_latest_descriptor()
    latest_fields = dict(latest_descriptor["latest_execution_bridge_health"]["recommended_fields"])
    trend_fields = dict(latest_descriptor["execution_bridge_health_trend_summary"]["recommended_fields"])
    latest_example_values = {
        "reported_at": "2026-04-08T14:35:00+08:00",
        "source_id": primary_sample["source_id"],
        "deployment_role": primary_sample["deployment_role"],
        "bridge_path": primary_sample["bridge_path"],
        "overall_status": "healthy",
        "gateway_status": "healthy",
        "qmt_vm_status": "healthy",
        "summary_lines": ["主 Gateway 与 QMT VM 均在线。"],
    }
    trend_example_values = {
        "latest_reported_at": latest_example_values["reported_at"],
        "latest_source_id": primary_sample["source_id"],
        "latest_deployment_role": primary_sample["deployment_role"],
        "latest_bridge_path": primary_sample["bridge_path"],
        "trend_status": "stable",
        "latest_overall_status": "healthy",
        "health_trend_snapshot": {
            "latest_reported_at": latest_example_values["reported_at"],
            "latest_source_id": primary_sample["source_id"],
            "latest_deployment_role": primary_sample["deployment_role"],
            "latest_bridge_path": primary_sample["bridge_path"],
            "latest_overall_status": "healthy",
            "trend_status": "stable",
            "attention_ratio": 0.0,
            "latest_gateway_status": "healthy",
            "latest_qmt_vm_status": "healthy",
        },
        "summary_lines": [
            "Windows Execution Gateway 最近窗口稳定。",
            "QMT VM 最近窗口稳定。",
        ],
    }
    primary_request = build_execution_bridge_health_ingress_payload(
        trigger=trigger,
        source_id=primary_sample["source_id"],
        deployment_role=primary_sample["deployment_role"],
        bridge_path=primary_sample["bridge_path"],
        health={
            "reported_at": latest_example_values["reported_at"],
            "gateway_online": True,
            "qmt_connected": True,
            "overall_status": "healthy",
            "summary_lines": list(latest_example_values["summary_lines"]),
            "windows_execution_gateway": {
                "status": "healthy",
                "reachable": True,
                "latency_ms": 8.5,
            },
            "qmt_vm": {
                "status": "healthy",
                "reachable": True,
                "latency_ms": 15.0,
            },
        },
    )
    backup_request = build_execution_bridge_health_ingress_payload(
        trigger=trigger,
        source_id=backup_sample["source_id"],
        deployment_role=backup_sample["deployment_role"],
        bridge_path=backup_sample["bridge_path"],
        health={
            "reported_at": "2026-04-08T14:35:30+08:00",
            "gateway_online": True,
            "qmt_connected": False,
            "overall_status": "degraded",
            "summary_lines": ["备 Gateway 在线，但 QMT VM 当前未连接。"],
            "windows_execution_gateway": {
                "status": "healthy",
                "reachable": True,
                "latency_ms": 11.0,
            },
            "qmt_vm": {
                "status": "down",
                "reachable": False,
                "latency_ms": 0.0,
                "detail": "QMT 会话未建立。",
            },
        },
    )
    api_base = api_base_url.rstrip("/")
    minimal_request_body = client_template["minimal_request_body"]
    primary_curl_body = json.dumps(minimal_request_body, ensure_ascii=False)
    return {
        "request_samples": {
            "windows_gateway_minimal_post_body": minimal_request_body,
            "windows_gateway_primary_post_body": primary_request,
            "windows_gateway_backup_post_body": backup_request,
        },
        "read_samples": {
            "linux_latest_read_example": {
                "root_key": latest_descriptor["latest_execution_bridge_health"]["root_key"],
                "recommended_fields": latest_fields,
                "example_values": latest_example_values,
            },
            "linux_trend_read_example": {
                "root_key": latest_descriptor["execution_bridge_health_trend_summary"]["root_key"],
                "recommended_fields": trend_fields,
                "example_values": trend_example_values,
            },
        },
        "http_samples": {
            "windows_gateway_post": {
                "method": client_template["method"],
                "path": client_template["path"],
                "content_type": client_template["content_type"],
                "body": minimal_request_body,
            },
            "curl_post_example": (
                f"curl -X POST \"{api_base}{client_template['path']}\" "
                "-H \"Content-Type: application/json\" "
                f"-d '{primary_curl_body}'"
            ),
        },
        "source_value_samples": {
            "linux_openclaw": latest_descriptor["source_value_suggestions"]["linux_openclaw"],
            "windows_gateway_primary": latest_descriptor["source_value_suggestions"]["windows_gateway_primary"],
            "windows_gateway_backup": latest_descriptor["source_value_suggestions"]["windows_gateway_backup"],
        },
        "related_helpers": {
            "client_template": client_template,
            "backup_client_template": backup_client_template,
            "latest_descriptor": latest_descriptor,
        },
    }


def build_execution_bridge_health_ingress_request_template(
    *,
    trigger: str = "windows_gateway",
    source_id: str = "",
    deployment_role: str = "",
    bridge_path: str = "",
) -> dict:
    """返回 execution bridge 健康上报最小请求体模板。"""

    return build_execution_bridge_health_ingress_payload(
        health={
            "reported_at": "",
            "gateway_online": False,
            "qmt_connected": False,
            "summary_lines": ["Windows Execution Gateway 健康快照缺失。"],
        },
        trigger=trigger,
        source_id=source_id,
        deployment_role=deployment_role,
        bridge_path=bridge_path,
    )


def get_execution_bridge_health_default_fields() -> dict:
    """返回 health 顶层默认字段（稳定空契约）。"""

    return deepcopy(_empty_execution_bridge_health_payload())


def get_execution_bridge_health_latest_helper(latest: dict | None = None, trend: dict | None = None) -> dict:
    """从 latest/trend 中抽取 Main 推荐读取字段。"""

    latest_payload = dict(latest or {})
    latest_health = dict(latest_payload.get("health") or {})
    trend_payload = dict(trend or {})
    if not latest_health:
        latest_health = get_execution_bridge_health_default_fields()
    return {
        "latest_source": {
            "reported_at": str(latest_health.get("reported_at") or ""),
            "source_id": str(latest_health.get("source_id") or ""),
            "deployment_role": str(latest_health.get("deployment_role") or ""),
            "bridge_path": str(latest_health.get("bridge_path") or ""),
        },
        "latest_status": {
            "overall_status": str(latest_health.get("overall_status") or "unknown"),
            "gateway_status": str(((latest_health.get("windows_execution_gateway") or {}).get("status")) or "unknown"),
            "qmt_vm_status": str(((latest_health.get("qmt_vm") or {}).get("status")) or "unknown"),
            "gateway_online": bool(latest_health.get("gateway_online", False)),
            "qmt_connected": bool(latest_health.get("qmt_connected", False)),
        },
        "latest_trend": {
            "trend_status": str(trend_payload.get("trend_status") or "unknown"),
            "attention_ratio": float(trend_payload.get("attention_ratio", 0.0) or 0.0),
            "latest_reported_at": str(trend_payload.get("latest_reported_at") or ""),
            "latest_source_id": str(trend_payload.get("latest_source_id") or ""),
            "latest_deployment_role": str(trend_payload.get("latest_deployment_role") or ""),
            "latest_bridge_path": str(trend_payload.get("latest_bridge_path") or ""),
        },
        "recommended_field_paths": [
            "latest_execution_bridge_health.health.reported_at",
            "latest_execution_bridge_health.health.source_id",
            "latest_execution_bridge_health.health.deployment_role",
            "latest_execution_bridge_health.health.bridge_path",
            "latest_execution_bridge_health.health.overall_status",
            "latest_execution_bridge_health.health.windows_execution_gateway.status",
            "latest_execution_bridge_health.health.qmt_vm.status",
            "execution_bridge_health_trend_summary.trend_status",
            "execution_bridge_health_trend_summary.attention_ratio",
        ],
    }


def get_execution_bridge_source_field_recommendations() -> dict:
    """返回 Linux/OpenClaw 与 Windows Gateway 的 source 字段取值建议。"""

    return {
        "source_id": {
            "linux_openclaw": "openclaw-linux-01",
            "windows_gateway": "windows-gateway-vm-01",
            "rule": "建议使用稳定实例 ID，便于跨日追踪与告警聚合。",
        },
        "deployment_role": {
            "linux_openclaw": "decision_brain",
            "windows_gateway": "primary_gateway",
            "alternatives": ["backup_gateway", "dr_gateway"],
            "rule": "建议描述部署角色而非机器名，支持主备切换。",
        },
        "bridge_path": {
            "linux_openclaw": "openclaw_linux -> windows_gateway -> qmt_vm",
            "windows_gateway": "openclaw_linux -> windows_gateway -> qmt_vm",
            "rule": "建议固定链路命名，主备切换仅替换中间节点名称。",
        },
    }


class WatchSnapshotItem(BaseModel):
    symbol: str
    name: str = ""
    last_price: float
    pre_close: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0


class MonitorHeartbeat(BaseModel):
    heartbeat_id: str
    generated_at: str
    expires_at: str
    trigger: str
    phase: str
    staleness_level: str = "fresh"
    symbol_count: int
    alert_count: int
    items: list[WatchSnapshotItem] = Field(default_factory=list)


class MonitorEventRecord(BaseModel):
    event_id: str
    event_at: str
    event_source: str = "alert"
    symbol: str
    name: str = ""
    alert_type: str
    message: str
    severity: str
    price: float
    change_pct: float


class MonitorPoolSnapshot(BaseModel):
    snapshot_id: str
    generated_at: str
    trade_date: str
    source: str
    discussion_state: str | None = None
    pool_state: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    candidate_pool: list[dict] = Field(default_factory=list)
    focus_pool: list[dict] = Field(default_factory=list)
    execution_pool: list[dict] = Field(default_factory=list)
    watchlist: list[dict] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)


class MonitorExitSnapshotRecord(BaseModel):
    snapshot_id: str
    generated_at: str
    trigger: str
    payload: dict = Field(default_factory=dict)


class MonitorExecutionBridgeHealthRecord(BaseModel):
    health_id: str
    generated_at: str
    trigger: str
    payload: dict = Field(default_factory=dict)


class MonitorPositionWatchRecord(BaseModel):
    watch_id: str
    generated_at: str
    trigger: str
    payload: dict = Field(default_factory=dict)


class MonitorStateService:
    """盯盘心跳与事件持久化服务。"""

    def __init__(
        self,
        state_store: StateStore,
        config_mgr: RuntimeConfigManager | None = None,
        archive_store: DataArchiveStore | None = None,
        now_factory=None,
    ) -> None:
        self._state_store = state_store
        self._config_mgr = config_mgr
        self._archive_store = archive_store
        self._now_factory = now_factory or datetime.now

    def save_heartbeat_if_due(
        self,
        snapshots: list[QuoteSnapshot],
        alerts: list[AlertEvent] | None = None,
        trigger: str = "scheduler",
        phase: str | None = None,
    ) -> dict | None:
        now = self._now_factory()
        resolved_phase = phase or self._resolve_phase(now)
        interval = self._heartbeat_interval_seconds(resolved_phase)
        latest = self._state_store.get("latest_heartbeat")
        if latest:
            last_at = datetime.fromisoformat(latest["generated_at"])
            if (now - last_at).total_seconds() < interval:
                return None

        heartbeat = MonitorHeartbeat(
            heartbeat_id=f"heartbeat-{now.strftime('%Y%m%d%H%M%S')}",
            generated_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=interval)).isoformat(),
            trigger=trigger,
            phase=resolved_phase,
            symbol_count=len(snapshots),
            alert_count=len(alerts or []),
            items=self._build_items(snapshots),
        )
        heartbeat_payload = heartbeat.model_dump()
        history = self._state_store.get("heartbeat_history", [])
        history.append(
            {
                "heartbeat_id": heartbeat.heartbeat_id,
                "generated_at": heartbeat.generated_at,
                "expires_at": heartbeat.expires_at,
                "trigger": heartbeat.trigger,
                "phase": heartbeat.phase,
                "staleness_level": heartbeat.staleness_level,
                "symbol_count": heartbeat.symbol_count,
                "alert_count": heartbeat.alert_count,
            }
        )
        self._state_store.set("latest_heartbeat", heartbeat_payload)
        self._state_store.set("heartbeat_history", history[-200:])
        self._persist_monitor_context()
        logger.info("monitor heartbeat saved: phase=%s symbols=%d alerts=%d", resolved_phase, len(snapshots), len(alerts or []))
        return heartbeat_payload

    def record_alert_events(self, alerts: list[AlertEvent], snapshots: list[QuoteSnapshot] | None = None) -> list[dict]:
        if not alerts:
            return []
        now = self._now_factory()
        snapshot_map = {item.symbol: item for item in snapshots or []}
        recorded: list[dict] = []

        for index, alert in enumerate(alerts, start=1):
            snap = snapshot_map.get(alert.symbol)
            record = MonitorEventRecord(
                event_id=f"monitor-event-{now.strftime('%Y%m%d%H%M%S')}-{index}",
                event_at=now.isoformat(),
                event_source="alert",
                symbol=alert.symbol,
                name=(snap.name if snap else ""),
                alert_type=alert.alert_type,
                message=alert.message,
                severity=alert.severity,
                price=alert.price,
                change_pct=alert.change_pct,
            )
            recorded.append(record.model_dump())
        appended = self._append_events(recorded)
        if appended:
            self._persist_monitor_context()
        return appended

    def get_state(self, event_limit: int = 20) -> dict:
        events = self._state_store.get("alert_events", [])
        latest = self._state_store.get("latest_heartbeat")
        history = self._state_store.get("heartbeat_history", [])
        latest_pool_snapshot = self._state_store.get("latest_pool_snapshot")
        pool_snapshot_history = self._state_store.get("pool_snapshot_history", [])
        latest_exit_snapshot = self._state_store.get("latest_exit_snapshot")
        exit_snapshot_history = self._state_store.get("exit_snapshot_history", [])
        latest_position_watch_snapshot = self._state_store.get("latest_position_watch_snapshot")
        position_watch_history = self._state_store.get("position_watch_history", [])
        execution_bridge_health_history = self._state_store.get("execution_bridge_health_history", [])
        freshness = self.get_heartbeat_freshness()
        return {
            "latest_heartbeat": latest,
            "heartbeat_history": history[-20:],
            "latest_pool_snapshot": latest_pool_snapshot,
            "pool_snapshot_history": pool_snapshot_history[-20:],
            "latest_exit_snapshot": latest_exit_snapshot,
            "exit_snapshot_history": exit_snapshot_history[-20:],
            "exit_snapshot_trend_summary": self.get_exit_snapshot_trend_summary(),
            "latest_position_watch_snapshot": latest_position_watch_snapshot,
            "position_watch_history": position_watch_history[-20:],
            "latest_execution_bridge_health": self.get_latest_execution_bridge_health(),
            "execution_bridge_health_history": execution_bridge_health_history[-20:],
            "execution_bridge_health_trend_summary": self.get_execution_bridge_health_trend_summary(),
            "recent_events": events[-event_limit:],
            "event_count": len(events),
            "heartbeat_freshness": freshness,
            "polling_status": self.get_polling_status(),
        }

    def get_latest_exit_snapshot(self) -> dict:
        latest = self._state_store.get("latest_exit_snapshot") or {}
        return {
            "snapshot_id": str(latest.get("snapshot_id") or ""),
            "generated_at": str(latest.get("generated_at") or ""),
            "trigger": str(latest.get("trigger") or ""),
            "snapshot": self._normalize_exit_snapshot(latest.get("snapshot")),
        }

    def save_exit_snapshot(self, snapshot: dict, trigger: str = "market_watcher") -> dict:
        now = self._now_factory()
        normalized_snapshot = self._normalize_exit_snapshot(snapshot)
        reason_counts = {
            str(item.get("key") or ""): int(item.get("count", 0) or 0)
            for item in normalized_snapshot.get("by_reason", [])
            if str(item.get("key") or "")
        }
        record = MonitorExitSnapshotRecord(
            snapshot_id=f"exit-snapshot-{now.strftime('%Y%m%d%H%M%S')}",
            generated_at=now.isoformat(),
            trigger=trigger,
            payload=normalized_snapshot,
        )
        latest_payload = {
            "snapshot_id": record.snapshot_id,
            "generated_at": record.generated_at,
            "trigger": record.trigger,
            "snapshot": record.payload,
        }
        history = self._state_store.get("exit_snapshot_history", [])
        history.append(
            {
                "snapshot_id": record.snapshot_id,
                "generated_at": record.generated_at,
                "trigger": record.trigger,
                "signal_count": int((record.payload or {}).get("signal_count", 0) or 0),
                "checked_at": float((record.payload or {}).get("checked_at", 0.0) or 0.0),
                "reason_counts": reason_counts,
            }
        )
        self._state_store.set("latest_exit_snapshot", latest_payload)
        self._state_store.set("exit_snapshot_history", history[-200:])
        self._persist_monitor_context()
        return latest_payload

    def get_latest_position_watch_snapshot(self) -> dict:
        latest = self._state_store.get("latest_position_watch_snapshot") or {}
        return {
            "watch_id": str(latest.get("watch_id") or ""),
            "generated_at": str(latest.get("generated_at") or ""),
            "trigger": str(latest.get("trigger") or ""),
            "payload": dict(latest.get("payload") or {}),
        }

    def save_position_watch_snapshot(self, snapshot: dict, trigger: str = "position_watch") -> dict:
        now = self._now_factory()
        normalized_snapshot = dict(snapshot or {})
        record = MonitorPositionWatchRecord(
            watch_id=f"position-watch-{now.strftime('%Y%m%d%H%M%S')}",
            generated_at=now.isoformat(),
            trigger=trigger,
            payload=normalized_snapshot,
        )
        latest_payload = {
            "watch_id": record.watch_id,
            "generated_at": record.generated_at,
            "trigger": record.trigger,
            "payload": record.payload,
        }
        history = self._state_store.get("position_watch_history", [])
        history.append(
            {
                "watch_id": record.watch_id,
                "generated_at": record.generated_at,
                "trigger": record.trigger,
                "trade_date": str(record.payload.get("trade_date") or ""),
                "mode": str(record.payload.get("mode") or ""),
                "position_count": int(record.payload.get("position_count", 0) or 0),
                "sell_signal_count": int(record.payload.get("sell_signal_count", 0) or 0),
                "day_trading_signal_count": int(record.payload.get("day_trading_signal_count", 0) or 0),
                "submitted_count": int(record.payload.get("submitted_count", 0) or 0),
                "queued_count": int(record.payload.get("queued_count", 0) or 0),
                "preview_count": int(record.payload.get("preview_count", 0) or 0),
            }
        )
        self._state_store.set("latest_position_watch_snapshot", latest_payload)
        self._state_store.set("position_watch_history", history[-720:])
        self._persist_monitor_context(trade_date=str(record.payload.get("trade_date") or "") or None)
        return latest_payload

    def get_exit_snapshot_trend_summary(self, recent_limit: int = 20) -> dict:
        history = list(self._state_store.get("exit_snapshot_history", []))
        if recent_limit > 0:
            history = history[-recent_limit:]
        if not history:
            return {
                "available": False,
                "recent_limit": recent_limit,
                "snapshot_count": 0,
                "non_zero_snapshot_count": 0,
                "latest_signal_count": 0,
                "total_signals": 0,
                "avg_signal_count": 0.0,
                "max_signal_count": 0,
                "signal_count_series": [],
                "by_reason": [],
                "summary_lines": ["最近无 exit snapshot 历史记录。"],
            }
        signal_count_series = [int(item.get("signal_count", 0) or 0) for item in history]
        snapshot_count = len(signal_count_series)
        total_signals = sum(signal_count_series)
        non_zero_snapshot_count = sum(1 for value in signal_count_series if value > 0)
        latest_signal_count = signal_count_series[-1]
        avg_signal_count = round(total_signals / snapshot_count, 6)
        max_signal_count = max(signal_count_series)
        reason_counter: dict[str, int] = {}
        for item in history:
            for reason, count in dict(item.get("reason_counts") or {}).items():
                reason_key = str(reason or "")
                if not reason_key:
                    continue
                reason_counter[reason_key] = reason_counter.get(reason_key, 0) + int(count or 0)
        by_reason = [
            {"key": key, "count": count}
            for key, count in sorted(reason_counter.items(), key=lambda pair: (-pair[1], pair[0]))
        ]
        summary_lines = [
            (
                f"最近 {snapshot_count} 次 exit snapshot，累计信号 {total_signals} 条，"
                f"平均每次 {avg_signal_count:.2f} 条，最近一次 {latest_signal_count} 条。"
            )
        ]
        if by_reason:
            summary_lines.append(f"主要原因: {by_reason[0]['key']}({by_reason[0]['count']})。")
        return {
            "available": True,
            "recent_limit": recent_limit,
            "snapshot_count": snapshot_count,
            "non_zero_snapshot_count": non_zero_snapshot_count,
            "latest_signal_count": latest_signal_count,
            "total_signals": total_signals,
            "avg_signal_count": avg_signal_count,
            "max_signal_count": max_signal_count,
            "signal_count_series": signal_count_series,
            "by_reason": by_reason,
            "summary_lines": summary_lines,
        }

    def get_latest_execution_bridge_health(self) -> dict:
        latest = self._state_store.get("latest_execution_bridge_health") or {}
        return {
            "health_id": str(latest.get("health_id") or ""),
            "generated_at": str(latest.get("generated_at") or ""),
            "trigger": str(latest.get("trigger") or ""),
            "health": self._normalize_execution_bridge_health(latest.get("health")),
        }

    def save_execution_bridge_health(self, health: dict, trigger: str = "windows_gateway") -> dict:
        now = self._now_factory()
        normalized_health = self._normalize_execution_bridge_health(health)
        record = MonitorExecutionBridgeHealthRecord(
            health_id=f"execution-health-{now.strftime('%Y%m%d%H%M%S')}",
            generated_at=now.isoformat(),
            trigger=trigger,
            payload=normalized_health,
        )
        latest_payload = {
            "health_id": record.health_id,
            "generated_at": record.generated_at,
            "trigger": record.trigger,
            "health": record.payload,
        }
        history = self._state_store.get("execution_bridge_health_history", [])
        history.append(
            {
                "health_id": record.health_id,
                "generated_at": record.generated_at,
                "trigger": record.trigger,
                "checked_at": float((record.payload or {}).get("checked_at", 0.0) or 0.0),
                "reported_at": str((record.payload or {}).get("reported_at") or ""),
                "source_id": str((record.payload or {}).get("source_id") or ""),
                "deployment_role": str((record.payload or {}).get("deployment_role") or ""),
                "bridge_path": str((record.payload or {}).get("bridge_path") or ""),
                "overall_status": str((record.payload or {}).get("overall_status") or "unknown"),
                "gateway_online": bool((record.payload or {}).get("gateway_online", False)),
                "qmt_connected": bool((record.payload or {}).get("qmt_connected", False)),
                "session_fresh_seconds": int((record.payload or {}).get("session_fresh_seconds", 0) or 0),
                "last_error": str((record.payload or {}).get("last_error") or ""),
                "attention_components": list((record.payload or {}).get("attention_components", [])),
                "attention_component_keys": list((record.payload or {}).get("attention_component_keys", [])),
                "windows_execution_gateway": {
                    "status": str((((record.payload or {}).get("windows_execution_gateway") or {}).get("status")) or "unknown"),
                    "reachable": bool((((record.payload or {}).get("windows_execution_gateway") or {}).get("reachable", False))),
                    "latency_ms": float((((record.payload or {}).get("windows_execution_gateway") or {}).get("latency_ms", 0.0)) or 0.0),
                    "staleness_seconds": float(
                        ((((record.payload or {}).get("windows_execution_gateway") or {}).get("staleness_seconds", 0.0))) or 0.0
                    ),
                    "error_count": int((((record.payload or {}).get("windows_execution_gateway") or {}).get("error_count", 0)) or 0),
                },
                "qmt_vm": {
                    "status": str((((record.payload or {}).get("qmt_vm") or {}).get("status")) or "unknown"),
                    "reachable": bool((((record.payload or {}).get("qmt_vm") or {}).get("reachable", False))),
                    "latency_ms": float((((record.payload or {}).get("qmt_vm") or {}).get("latency_ms", 0.0)) or 0.0),
                    "staleness_seconds": float((((record.payload or {}).get("qmt_vm") or {}).get("staleness_seconds", 0.0)) or 0.0),
                    "error_count": int((((record.payload or {}).get("qmt_vm") or {}).get("error_count", 0)) or 0),
                },
            }
        )
        self._state_store.set("latest_execution_bridge_health", latest_payload)
        self._state_store.set("execution_bridge_health_history", history[-200:])
        self._persist_monitor_context()
        return latest_payload

    def get_execution_bridge_health_trend_summary(self, recent_limit: int = 20) -> dict:
        history = list(self._state_store.get("execution_bridge_health_history", []))
        if recent_limit > 0:
            history = history[-recent_limit:]
        if not history:
            gateway_trend = self._empty_execution_component_trend_summary(
                "windows_execution_gateway",
                "Windows Execution Gateway",
            )
            qmt_vm_trend = self._empty_execution_component_trend_summary("qmt_vm", "QMT VM")
            return {
                "available": False,
                "recent_limit": recent_limit,
                "snapshot_count": 0,
                "latest_reported_at": "",
                "latest_source_id": "",
                "latest_deployment_role": "",
                "latest_bridge_path": "",
                "latest_overall_status": "unknown",
                "overall_status_series": [],
                "overall_status_counts": {"healthy": 0, "degraded": 0, "down": 0, "unknown": 0},
                "trend_status": "unknown",
                "gateway_online_ratio": 0.0,
                "qmt_connected_ratio": 0.0,
                "latest_gateway_online": False,
                "latest_qmt_connected": False,
                "latest_gateway_status": "unknown",
                "latest_qmt_vm_status": "unknown",
                "latest_session_fresh_seconds": 0,
                "last_error_count": 0,
                "attention_snapshot_count": 0,
                "attention_ratio": 0.0,
                "latest_attention_components": [],
                "latest_attention_component_keys": [],
                "windows_execution_gateway": gateway_trend,
                "qmt_vm": qmt_vm_trend,
                "component_trends": [gateway_trend, qmt_vm_trend],
                "health_trend_snapshot": {
                    "latest_reported_at": "",
                    "latest_source_id": "",
                    "latest_deployment_role": "",
                    "latest_bridge_path": "",
                    "latest_overall_status": "unknown",
                    "trend_status": "unknown",
                    "attention_ratio": 0.0,
                    "latest_gateway_status": "unknown",
                    "latest_qmt_vm_status": "unknown",
                },
                "summary_lines": ["最近无 execution bridge health 历史记录。"],
            }
        snapshot_count = len(history)
        gateway_online_count = sum(1 for item in history if bool(item.get("gateway_online", False)))
        qmt_connected_count = sum(1 for item in history if bool(item.get("qmt_connected", False)))
        last_error_count = sum(1 for item in history if str(item.get("last_error") or ""))
        latest = history[-1]
        latest_reported_at = str(latest.get("reported_at") or "")
        latest_source_id = str(latest.get("source_id") or "")
        latest_deployment_role = str(latest.get("deployment_role") or "")
        latest_bridge_path = str(latest.get("bridge_path") or "")
        overall_status_series = [self._normalize_execution_bridge_status(item.get("overall_status")) for item in history]
        overall_status_counts = {
            "healthy": sum(1 for status in overall_status_series if status == "healthy"),
            "degraded": sum(1 for status in overall_status_series if status == "degraded"),
            "down": sum(1 for status in overall_status_series if status == "down"),
            "unknown": sum(1 for status in overall_status_series if status == "unknown"),
        }
        latest_overall_status = overall_status_series[-1]
        trend_status = self._classify_execution_bridge_trend(overall_status_series)
        attention_snapshot_count = sum(1 for status in overall_status_series if status in {"degraded", "down"})
        attention_ratio = round(attention_snapshot_count / snapshot_count, 6)
        gateway_online_ratio = round(gateway_online_count / snapshot_count, 6)
        qmt_connected_ratio = round(qmt_connected_count / snapshot_count, 6)
        latest_session_fresh_seconds = int(latest.get("session_fresh_seconds", 0) or 0)
        latest_attention_components = [str(item) for item in list(latest.get("attention_components") or []) if str(item)]
        latest_attention_component_keys = [str(item) for item in list(latest.get("attention_component_keys") or []) if str(item)]
        gateway_trend = self._build_execution_component_trend_summary(
            history,
            key="windows_execution_gateway",
            label="Windows Execution Gateway",
        )
        qmt_vm_trend = self._build_execution_component_trend_summary(
            history,
            key="qmt_vm",
            label="QMT VM",
        )
        summary_lines = [
            (
                f"最近 {snapshot_count} 次执行面健康快照，整体趋势={trend_status}，"
                f"最新状态={latest_overall_status}。"
            ),
            (
                f"Gateway 在线率 {gateway_online_ratio:.2%}，QMT 连通率 {qmt_connected_ratio:.2%}；"
                f"最新 Gateway={gateway_trend['latest_status']}，QMT VM={qmt_vm_trend['latest_status']}。"
            ),
        ]
        if latest_session_fresh_seconds > 0:
            summary_lines.append(f"最近一次会话新鲜度 {latest_session_fresh_seconds} 秒。")
        if attention_snapshot_count > 0:
            summary_lines.append(f"最近窗口内出现需关注快照 {attention_snapshot_count} 次。")
        if last_error_count > 0:
            summary_lines.append(f"最近窗口内出现错误快照 {last_error_count} 次。")
        health_trend_snapshot = {
            "latest_reported_at": latest_reported_at,
            "latest_source_id": latest_source_id,
            "latest_deployment_role": latest_deployment_role,
            "latest_bridge_path": latest_bridge_path,
            "latest_overall_status": latest_overall_status,
            "trend_status": trend_status,
            "attention_ratio": attention_ratio,
            "latest_gateway_status": gateway_trend["latest_status"],
            "latest_qmt_vm_status": qmt_vm_trend["latest_status"],
        }
        return {
            "available": True,
            "recent_limit": recent_limit,
            "snapshot_count": snapshot_count,
            "latest_reported_at": latest_reported_at,
            "latest_source_id": latest_source_id,
            "latest_deployment_role": latest_deployment_role,
            "latest_bridge_path": latest_bridge_path,
            "latest_overall_status": latest_overall_status,
            "overall_status_series": overall_status_series,
            "overall_status_counts": overall_status_counts,
            "trend_status": trend_status,
            "gateway_online_ratio": gateway_online_ratio,
            "qmt_connected_ratio": qmt_connected_ratio,
            "latest_gateway_online": bool(latest.get("gateway_online", False)),
            "latest_qmt_connected": bool(latest.get("qmt_connected", False)),
            "latest_gateway_status": gateway_trend["latest_status"],
            "latest_qmt_vm_status": qmt_vm_trend["latest_status"],
            "latest_session_fresh_seconds": latest_session_fresh_seconds,
            "last_error_count": last_error_count,
            "attention_snapshot_count": attention_snapshot_count,
            "attention_ratio": attention_ratio,
            "latest_attention_components": latest_attention_components,
            "latest_attention_component_keys": latest_attention_component_keys,
            "windows_execution_gateway": gateway_trend,
            "qmt_vm": qmt_vm_trend,
            "component_trends": [gateway_trend, qmt_vm_trend],
            "health_trend_snapshot": health_trend_snapshot,
            "summary_lines": summary_lines,
        }

    @staticmethod
    def _normalize_execution_bridge_status(status: str | None) -> str:
        normalized = str(status or "unknown").strip().lower()
        if normalized in EXECUTION_HEALTH_STATUS_SCORES:
            return normalized
        if normalized in {"ok", "up", "online", "connected", "ready"}:
            return "healthy"
        if normalized in {"warn", "warning", "limited"}:
            return "degraded"
        if normalized in {"offline", "disconnected", "failed", "error"}:
            return "down"
        return "unknown"

    @classmethod
    def _classify_execution_bridge_trend(cls, status_series: list[str]) -> str:
        if not status_series:
            return "unknown"
        scores = [EXECUTION_HEALTH_STATUS_SCORES.get(cls._normalize_execution_bridge_status(item), 1) for item in status_series]
        avg_score = sum(scores) / len(scores)
        if avg_score >= 2.5:
            return "stable"
        if avg_score >= 1.5:
            return "degrading"
        return "critical"

    @staticmethod
    def _empty_execution_component_trend_summary(key: str, label: str) -> dict:
        return {
            "key": key,
            "label": label,
            "latest_status": "unknown",
            "latest_reachable": False,
            "status_series": [],
            "reachable_ratio": 0.0,
            "avg_latency_ms": 0.0,
            "max_latency_ms": 0.0,
            "max_staleness_seconds": 0.0,
            "error_count_total": 0,
            "attention_count": 0,
            "summary_lines": [f"{label} 最近无健康历史。"],
        }

    @classmethod
    def _build_execution_component_trend_summary(self, history: list[dict], key: str, label: str) -> dict:
        if not history:
            return self._empty_execution_component_trend_summary(key, label)
        component_history = [dict(item.get(key) or {}) for item in history]
        if not component_history:
            return self._empty_execution_component_trend_summary(key, label)

        latest = component_history[-1]
        status_series = [self._normalize_execution_bridge_status(item.get("status")) for item in component_history]
        reachable_values = [bool(item.get("reachable", False)) for item in component_history]
        reachable_ratio = round(sum(1 for value in reachable_values if value) / len(reachable_values), 6)
        latency_values = [float(item.get("latency_ms", 0.0) or 0.0) for item in component_history]
        avg_latency_ms = round(sum(latency_values) / len(latency_values), 6)
        max_latency_ms = max(latency_values) if latency_values else 0.0
        staleness_values = [float(item.get("staleness_seconds", 0.0) or 0.0) for item in component_history]
        max_staleness_seconds = max(staleness_values) if staleness_values else 0.0
        error_count_total = sum(int(item.get("error_count", 0) or 0) for item in component_history)
        attention_count = sum(1 for status in status_series if status in {"degraded", "down"})
        latest_status = status_series[-1]
        latest_reachable = bool(latest.get("reachable", False))
        summary_lines = [
            f"{label} 最近状态={latest_status}，可达率={reachable_ratio:.2%}，平均延迟={avg_latency_ms:.2f}ms。"
        ]
        if attention_count > 0:
            summary_lines.append(f"{label} 最近窗口内需关注状态 {attention_count} 次。")
        return {
            "key": key,
            "label": label,
            "latest_status": latest_status,
            "latest_reachable": latest_reachable,
            "status_series": status_series,
            "reachable_ratio": reachable_ratio,
            "avg_latency_ms": avg_latency_ms,
            "max_latency_ms": max_latency_ms,
            "max_staleness_seconds": max_staleness_seconds,
            "error_count_total": error_count_total,
            "attention_count": attention_count,
            "summary_lines": summary_lines,
        }

    def _derive_execution_bridge_overall_status(self, gateway_status: str, qmt_status: str) -> str:
        gateway = self._normalize_execution_bridge_status(gateway_status)
        qmt = self._normalize_execution_bridge_status(qmt_status)
        if "down" in {gateway, qmt}:
            return "down"
        if "degraded" in {gateway, qmt}:
            return "degraded"
        if gateway == "healthy" and qmt == "healthy":
            return "healthy"
        return "unknown"

    def _build_execution_bridge_summary_lines(self, payload: dict) -> list[str]:
        gateway = dict(payload.get("windows_execution_gateway") or {})
        qmt_vm = dict(payload.get("qmt_vm") or {})
        lines = [
            (
                f"执行面状态={payload.get('overall_status', 'unknown')}，"
                f"Gateway={gateway.get('status', 'unknown')}，QMT VM={qmt_vm.get('status', 'unknown')}。"
            )
        ]
        source_id = str(payload.get("source_id") or "")
        deployment_role = str(payload.get("deployment_role") or "")
        bridge_path = str(payload.get("bridge_path") or "")
        reported_at = str(payload.get("reported_at") or "")
        if source_id or deployment_role or bridge_path or reported_at:
            lines.append(
                "来源="
                + (source_id or "unknown")
                + (f" | 角色={deployment_role}" if deployment_role else "")
                + (f" | 桥路={bridge_path}" if bridge_path else "")
                + (f" | reported_at={reported_at}" if reported_at else "")
            )
        attention_components = list(payload.get("attention_components", []))
        if attention_components:
            lines.append(f"关注组件: {', '.join(attention_components)}。")
        last_error = str(payload.get("last_error") or "")
        if last_error:
            lines.append(f"最近错误: {last_error}")
        return lines

    @staticmethod
    def _build_execution_component_snapshot(component: dict) -> dict:
        data = dict(component or {})
        return {
            "key": str(data.get("key") or ""),
            "label": str(data.get("label") or ""),
            "status": str(data.get("status") or "unknown"),
            "reachable": bool(data.get("reachable", False)),
            "latency_ms": float(data.get("latency_ms", 0.0) or 0.0),
            "staleness_seconds": float(data.get("staleness_seconds", 0.0) or 0.0),
            "error_count": int(data.get("error_count", 0) or 0),
            "detail": str(data.get("detail") or ""),
            "tags": [str(item) for item in list(data.get("tags", [])) if str(item)],
        }

    @staticmethod
    def _resolve_execution_bridge_attention_components(
        attention_components: list,
        gateway_component: dict,
        qmt_component: dict,
    ) -> tuple[list[str], list[str]]:
        normalized_tokens = [str(item).strip() for item in attention_components if str(item).strip()]
        component_lookup = {
            "windows_execution_gateway": ("windows_execution_gateway", "Windows Execution Gateway"),
            "gateway": ("windows_execution_gateway", "Windows Execution Gateway"),
            "windows execution gateway": ("windows_execution_gateway", "Windows Execution Gateway"),
            "qmt_vm": ("qmt_vm", "QMT VM"),
            "qmt": ("qmt_vm", "QMT VM"),
            "qmt vm": ("qmt_vm", "QMT VM"),
            "qmt-vm": ("qmt_vm", "QMT VM"),
        }
        seen_keys: set[str] = set()
        labels: list[str] = []
        keys: list[str] = []

        def _append_attention(key: str, label: str) -> None:
            if key in seen_keys:
                return
            seen_keys.add(key)
            keys.append(key)
            labels.append(label)

        for token in normalized_tokens:
            resolved = component_lookup.get(token.lower())
            if resolved:
                _append_attention(resolved[0], resolved[1])
                continue
            if token == "Windows Execution Gateway":
                _append_attention("windows_execution_gateway", "Windows Execution Gateway")
                continue
            if token == "QMT VM":
                _append_attention("qmt_vm", "QMT VM")
                continue

        if not labels:
            for component in (gateway_component, qmt_component):
                status = str(component.get("status") or "unknown")
                if status in {"degraded", "down"}:
                    _append_attention(str(component.get("key") or ""), str(component.get("label") or ""))
        return labels, keys

    def mark_poll_if_due(self, layer: str, trigger: str, force: bool = False) -> dict:
        status = self.get_polling_status().get(layer)
        if status is None:
            raise ValueError(f"unsupported poll layer: {layer}")
        if not force and not status["due_now"]:
            return {
                "triggered": False,
                "layer": layer,
                "trigger": trigger,
                **status,
            }

        now = self._now_factory()
        interval_seconds = self._poll_interval_seconds(layer)
        state = self._state_store.get("polling_state", {})
        entry = {
            "layer": layer,
            "trigger": trigger,
            "last_polled_at": now.isoformat(),
            "next_due_at": (now + timedelta(seconds=interval_seconds)).isoformat(),
            "interval_seconds": interval_seconds,
        }
        state[layer] = entry
        self._state_store.set("polling_state", state)
        self._persist_monitor_context()
        return {
            "triggered": True,
            "layer": layer,
            "trigger": trigger,
            "last_polled_at": entry["last_polled_at"],
            "next_due_at": entry["next_due_at"],
            "interval_seconds": interval_seconds,
            "due_now": False,
            "elapsed_seconds": 0,
        }

    def get_polling_status(self) -> dict[str, dict]:
        now = self._now_factory()
        state = self._state_store.get("polling_state", {})
        result: dict[str, dict] = {}
        for layer in ("candidate", "focus", "execution"):
            interval_seconds = self._poll_interval_seconds(layer)
            entry = state.get(layer, {})
            last_polled_at = entry.get("last_polled_at")
            elapsed_seconds = None
            due_now = True
            next_due_at = None
            if last_polled_at:
                elapsed = (now - datetime.fromisoformat(last_polled_at)).total_seconds()
                elapsed_seconds = max(int(elapsed), 0)
                due_now = elapsed >= interval_seconds
                next_due_at = (datetime.fromisoformat(last_polled_at) + timedelta(seconds=interval_seconds)).isoformat()
            result[layer] = {
                "layer": layer,
                "last_polled_at": last_polled_at,
                "next_due_at": next_due_at,
                "interval_seconds": interval_seconds,
                "elapsed_seconds": elapsed_seconds,
                "due_now": due_now,
                "last_trigger": entry.get("trigger"),
            }
        return result

    def build_change_summary(self, event_limit: int = 20) -> dict:
        events = self._state_store.get("alert_events", [])
        recent = list(reversed(events[-event_limit:]))
        state_changes = [item for item in recent if item.get("event_source") == "state_change"]
        type_counts: dict[str, int] = {}
        lines: list[str] = []
        for item in state_changes:
            alert_type = item["alert_type"]
            type_counts[alert_type] = type_counts.get(alert_type, 0) + 1
            lines.append(self._format_change_line(item))
        title = "当前无结构化池层变化"
        if state_changes:
            title = f"最近 {len(state_changes)} 条结构化变化"
        notify_level, should_notify = self._determine_notify_policy(type_counts)
        dispatch_title = {
            "critical": "执行池关键变化",
            "warning": "池层状态变化提醒",
            "info": "候选排序变化提醒",
            "none": "当前无结构化池层变化",
        }[notify_level]
        return {
            "title": title,
            "count": len(state_changes),
            "type_counts": type_counts,
            "items": state_changes,
            "lines": lines,
            "dispatch_title": dispatch_title,
            "notify_level": notify_level,
            "should_notify": should_notify,
            "summary_text": "\n".join([title, *lines]) if lines else title,
        }

    def should_dispatch_change_summary(self, signature: str, force: bool = False) -> tuple[bool, str]:
        latest = self._state_store.get("last_change_summary_dispatch")
        if not force and latest and latest.get("signature") == signature:
            return False, "duplicate"
        return True, "ready"

    def mark_change_summary_dispatched(self, signature: str, summary: dict) -> dict:
        now = self._now_factory().isoformat()
        payload = {
            "signature": signature,
            "dispatched_at": now,
            "dispatch_title": summary.get("dispatch_title"),
            "notify_level": summary.get("notify_level"),
            "count": summary.get("count", 0),
            "event_ids": [item.get("event_id") for item in summary.get("items", [])],
        }
        history = self._state_store.get("change_summary_dispatch_history", [])
        history.append(payload)
        self._state_store.set("last_change_summary_dispatch", payload)
        self._state_store.set("change_summary_dispatch_history", history[-100:])
        return payload

    def get_last_change_summary_dispatch(self) -> dict | None:
        return self._state_store.get("last_change_summary_dispatch")

    def save_pool_snapshot(
        self,
        trade_date: str,
        pool_snapshot: dict,
        source: str,
        discussion_state: str | None = None,
        pool_state: str | None = None,
    ) -> dict:
        now = self._now_factory()
        previous_snapshot = self._state_store.get("latest_pool_snapshot")
        snapshot = MonitorPoolSnapshot(
            snapshot_id=f"pool-snapshot-{now.strftime('%Y%m%d%H%M%S')}",
            generated_at=now.isoformat(),
            trade_date=trade_date,
            source=source,
            discussion_state=discussion_state,
            pool_state=pool_state,
            counts=pool_snapshot.get("counts", {}),
            candidate_pool=pool_snapshot.get("candidate_pool", []),
            focus_pool=pool_snapshot.get("focus_pool", []),
            execution_pool=pool_snapshot.get("execution_pool", []),
            watchlist=pool_snapshot.get("watchlist", []),
            rejected=pool_snapshot.get("rejected", []),
        )
        payload = snapshot.model_dump()
        change_events = self._build_pool_change_events(previous_snapshot, payload, now.isoformat())
        self._append_events(change_events)
        history = self._state_store.get("pool_snapshot_history", [])
        history.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "generated_at": snapshot.generated_at,
                "trade_date": snapshot.trade_date,
                "source": snapshot.source,
                "discussion_state": snapshot.discussion_state,
                "pool_state": snapshot.pool_state,
                "counts": snapshot.counts,
            }
        )
        self._state_store.set("latest_pool_snapshot", payload)
        self._state_store.set("pool_snapshot_history", history[-200:])
        self._persist_monitor_context(trade_date=trade_date)
        logger.info(
            "monitor pool snapshot saved: trade_date=%s candidate=%d focus=%d execution=%d",
            trade_date,
            snapshot.counts.get("candidate_pool", 0),
            snapshot.counts.get("focus_pool", 0),
            snapshot.counts.get("execution_pool", 0),
        )
        return payload

    def _runtime_config(self) -> RuntimeConfig:
        return self._config_mgr.get() if self._config_mgr else RuntimeConfig()

    def _normalize_exit_snapshot(self, snapshot: dict | None) -> dict:
        payload = _empty_exit_snapshot_payload()
        data = dict(snapshot or {})
        payload["version"] = str(data.get("version") or payload["version"])
        payload["checked_at"] = float(data.get("checked_at", payload["checked_at"]) or 0.0)
        payload["signal_count"] = int(data.get("signal_count", payload["signal_count"]) or 0)
        payload["watched_symbols"] = list(data.get("watched_symbols", payload["watched_symbols"]))
        payload["by_symbol"] = list(data.get("by_symbol", payload["by_symbol"]))
        payload["by_reason"] = list(data.get("by_reason", payload["by_reason"]))
        payload["by_severity"] = list(data.get("by_severity", payload["by_severity"]))
        payload["by_tag"] = list(data.get("by_tag", payload["by_tag"]))
        payload["summary_lines"] = list(data.get("summary_lines", payload["summary_lines"]))
        payload["items"] = list(data.get("items", payload["items"]))
        return payload

    def _normalize_execution_bridge_health(self, health: dict | None) -> dict:
        payload = _empty_execution_bridge_health_payload()
        data = dict(health or {})
        if not data:
            return payload
        windows_execution_gateway = dict(data.get("windows_execution_gateway") or {})
        qmt_vm = dict(data.get("qmt_vm") or {})
        payload["version"] = str(data.get("version") or payload["version"])
        payload["checked_at"] = float(data.get("checked_at", payload["checked_at"]) or 0.0)
        payload["reported_at"] = str(data.get("reported_at") or payload["reported_at"])
        payload["source_id"] = str(data.get("source_id") or payload["source_id"])
        payload["deployment_role"] = str(data.get("deployment_role") or payload["deployment_role"])
        payload["bridge_path"] = str(data.get("bridge_path") or payload["bridge_path"])
        payload["gateway_online"] = bool(data.get("gateway_online", payload["gateway_online"]))
        payload["qmt_connected"] = bool(data.get("qmt_connected", payload["qmt_connected"]))
        payload["account_id"] = str(data.get("account_id") or payload["account_id"])
        payload["session_fresh_seconds"] = int(data.get("session_fresh_seconds", payload["session_fresh_seconds"]) or 0)
        payload["last_poll_at"] = str(data.get("last_poll_at") or payload["last_poll_at"])
        payload["last_receipt_at"] = str(data.get("last_receipt_at") or payload["last_receipt_at"])
        payload["last_error"] = str(data.get("last_error") or payload["last_error"])

        gateway_status = self._normalize_execution_bridge_status(windows_execution_gateway.get("status"))
        if gateway_status == "unknown":
            if payload["gateway_online"]:
                gateway_status = "healthy"
                if int(windows_execution_gateway.get("error_count", 0) or 0) > 0:
                    gateway_status = "degraded"
            elif payload["last_error"] or windows_execution_gateway.get("last_error_at"):
                gateway_status = "down"

        qmt_status = self._normalize_execution_bridge_status(qmt_vm.get("status"))
        if qmt_status == "unknown":
            if payload["qmt_connected"]:
                qmt_status = "healthy"
                if int(qmt_vm.get("error_count", 0) or 0) > 0 or payload["session_fresh_seconds"] > 300:
                    qmt_status = "degraded"
            elif payload["last_error"] or qmt_vm.get("last_error_at"):
                qmt_status = "down"

        payload["windows_execution_gateway"]["status"] = gateway_status
        payload["windows_execution_gateway"]["reachable"] = bool(
            windows_execution_gateway.get("reachable", payload["gateway_online"])
        )
        payload["windows_execution_gateway"]["latency_ms"] = float(
            windows_execution_gateway.get("latency_ms", payload["windows_execution_gateway"]["latency_ms"]) or 0.0
        )
        payload["windows_execution_gateway"]["staleness_seconds"] = float(
            windows_execution_gateway.get("staleness_seconds", payload["windows_execution_gateway"]["staleness_seconds"]) or 0.0
        )
        payload["windows_execution_gateway"]["error_count"] = int(
            windows_execution_gateway.get("error_count", payload["windows_execution_gateway"]["error_count"]) or 0
        )
        payload["windows_execution_gateway"]["success_count"] = int(
            windows_execution_gateway.get("success_count", payload["windows_execution_gateway"]["success_count"]) or 0
        )
        payload["windows_execution_gateway"]["last_ok_at"] = str(
            windows_execution_gateway.get("last_ok_at") or payload["windows_execution_gateway"]["last_ok_at"]
        )
        payload["windows_execution_gateway"]["last_error_at"] = str(
            windows_execution_gateway.get("last_error_at") or payload["windows_execution_gateway"]["last_error_at"]
        )
        payload["windows_execution_gateway"]["detail"] = str(
            windows_execution_gateway.get("detail") or payload["last_error"] or payload["windows_execution_gateway"]["detail"]
        )
        payload["windows_execution_gateway"]["tags"] = [
            str(item) for item in list(windows_execution_gateway.get("tags", payload["windows_execution_gateway"]["tags"])) if str(item)
        ]
        payload["qmt_vm"]["status"] = qmt_status
        payload["qmt_vm"]["reachable"] = bool(qmt_vm.get("reachable", payload["qmt_connected"]))
        payload["qmt_vm"]["latency_ms"] = float(qmt_vm.get("latency_ms", payload["qmt_vm"]["latency_ms"]) or 0.0)
        payload["qmt_vm"]["staleness_seconds"] = float(
            qmt_vm.get("staleness_seconds", payload["session_fresh_seconds"]) or 0.0
        )
        payload["qmt_vm"]["error_count"] = int(qmt_vm.get("error_count", payload["qmt_vm"]["error_count"]) or 0)
        payload["qmt_vm"]["success_count"] = int(qmt_vm.get("success_count", payload["qmt_vm"]["success_count"]) or 0)
        payload["qmt_vm"]["last_ok_at"] = str(qmt_vm.get("last_ok_at") or payload["qmt_vm"]["last_ok_at"])
        payload["qmt_vm"]["last_error_at"] = str(qmt_vm.get("last_error_at") or payload["qmt_vm"]["last_error_at"])
        payload["qmt_vm"]["detail"] = str(qmt_vm.get("detail") or payload["last_error"] or payload["qmt_vm"]["detail"])
        payload["qmt_vm"]["tags"] = [str(item) for item in list(qmt_vm.get("tags", payload["qmt_vm"]["tags"])) if str(item)]
        payload["overall_status"] = self._normalize_execution_bridge_status(
            data.get("overall_status")
            or self._derive_execution_bridge_overall_status(
                payload["windows_execution_gateway"]["status"],
                payload["qmt_vm"]["status"],
            )
        )
        attention_components, attention_component_keys = self._resolve_execution_bridge_attention_components(
            list(data.get("attention_components", [])),
            payload["windows_execution_gateway"],
            payload["qmt_vm"],
        )
        payload["attention_components"] = attention_components
        payload["attention_component_keys"] = attention_component_keys
        payload["component_health"] = [
            self._build_execution_component_snapshot(payload["windows_execution_gateway"]),
            self._build_execution_component_snapshot(payload["qmt_vm"]),
        ]
        payload["summary_lines"] = list(data.get("summary_lines") or self._build_execution_bridge_summary_lines(payload))
        payload["updated_at"] = str(data.get("updated_at") or payload["updated_at"])
        return payload

    def _persist_monitor_context(self, trade_date: str | None = None) -> None:
        if not self._archive_store:
            return
        state = self.get_state(event_limit=20)
        latest_pool_snapshot = state.get("latest_pool_snapshot") or {}
        latest_heartbeat = state.get("latest_heartbeat") or {}
        resolved_trade_date = trade_date or latest_pool_snapshot.get("trade_date")
        if not resolved_trade_date and latest_heartbeat.get("generated_at"):
            resolved_trade_date = datetime.fromisoformat(latest_heartbeat["generated_at"]).date().isoformat()
        if not resolved_trade_date:
            return
        payload = {
            "available": True,
            "resource": "monitor_context",
            "trade_date": resolved_trade_date,
            "generated_at": self._now_factory().isoformat(),
            "latest_heartbeat": latest_heartbeat,
            "latest_pool_snapshot": latest_pool_snapshot,
            "latest_exit_snapshot": state.get("latest_exit_snapshot"),
            "exit_snapshot_history": state.get("exit_snapshot_history", []),
            "exit_snapshot_trend_summary": state.get("exit_snapshot_trend_summary", {}),
            "latest_position_watch_snapshot": state.get("latest_position_watch_snapshot"),
            "position_watch_history": state.get("position_watch_history", []),
            "latest_execution_bridge_health": state.get("latest_execution_bridge_health"),
            "execution_bridge_health_history": state.get("execution_bridge_health_history", []),
            "execution_bridge_health_trend_summary": state.get("execution_bridge_health_trend_summary", {}),
            "polling_status": state.get("polling_status", {}),
            "heartbeat_freshness": state.get("heartbeat_freshness"),
            "recent_events": state.get("recent_events", []),
            "event_count": state.get("event_count", 0),
        }
        self._archive_store.persist_monitor_context(resolved_trade_date, payload)

    def _append_events(self, events: list[dict]) -> list[dict]:
        if not events:
            return []
        now = self._now_factory()
        debounce_seconds = self._runtime_config().watch.event_debounce_seconds
        last_event_times = self._state_store.get("last_event_times", {})
        stored = self._state_store.get("alert_events", [])
        recorded: list[dict] = []
        for payload in events:
            key = f"{payload['symbol']}:{payload['alert_type']}:{payload['severity']}:{payload.get('event_source', 'alert')}"
            last_seen = last_event_times.get(key)
            if last_seen:
                elapsed = (now - datetime.fromisoformat(last_seen)).total_seconds()
                if elapsed < debounce_seconds:
                    continue
            stored.append(payload)
            recorded.append(payload)
            last_event_times[key] = payload["event_at"]
        if recorded:
            self._state_store.set("alert_events", stored[-500:])
            self._state_store.set("last_event_times", last_event_times)
            logger.info("monitor events recorded: %d", len(recorded))
        return recorded

    def _heartbeat_interval_seconds(self, phase: str) -> int:
        watch = self._runtime_config().watch
        return watch.auction_heartbeat_save_seconds if phase == "auction" else watch.heartbeat_save_seconds

    def _poll_interval_seconds(self, layer: str) -> int:
        watch = self._runtime_config().watch
        if layer == "candidate":
            return watch.candidate_poll_seconds
        if layer == "focus":
            return watch.focus_poll_seconds
        if layer == "execution":
            return watch.execution_poll_seconds
        raise ValueError(f"unsupported poll layer: {layer}")

    def _build_pool_change_events(self, previous: dict | None, current: dict, event_at: str) -> list[dict]:
        if not previous:
            return []
        events: list[dict] = []
        prev_candidate = {item["case_id"]: item for item in previous.get("candidate_pool", [])}
        curr_candidate = {item["case_id"]: item for item in current.get("candidate_pool", [])}

        prev_top3 = previous.get("candidate_pool", [])[:3]
        curr_top3 = current.get("candidate_pool", [])[:3]
        prev_ranks = {item["case_id"]: index + 1 for index, item in enumerate(prev_top3)}
        curr_ranks = {item["case_id"]: index + 1 for index, item in enumerate(curr_top3)}
        for case_id, rank in curr_ranks.items():
            prev_rank = prev_ranks.get(case_id)
            if prev_rank is None or prev_rank != rank:
                item = curr_candidate.get(case_id) or prev_candidate.get(case_id)
                if not item:
                    continue
                message = f"{item['symbol']} {item.get('name') or item['symbol']} 进入前3，当前第{rank}位"
                if prev_rank:
                    message = f"{item['symbol']} {item.get('name') or item['symbol']} 前3名次变化: {prev_rank} -> {rank}"
                events.append(
                    self._state_change_event(
                        event_at=event_at,
                        symbol=item["symbol"],
                        name=item.get("name", ""),
                        alert_type="top3_changed",
                        message=message,
                    )
                )

        prev_execution = {item["case_id"]: item for item in previous.get("execution_pool", [])}
        curr_execution = {item["case_id"]: item for item in current.get("execution_pool", [])}
        for case_id, item in curr_execution.items():
            if case_id not in prev_execution:
                events.append(
                    self._state_change_event(
                        event_at=event_at,
                        symbol=item["symbol"],
                        name=item.get("name", ""),
                        alert_type="execution_pool_changed",
                        message=f"{item['symbol']} {item.get('name') or item['symbol']} 进入 execution_pool",
                    )
                )
        for case_id, item in prev_execution.items():
            if case_id not in curr_execution:
                events.append(
                    self._state_change_event(
                        event_at=event_at,
                        symbol=item["symbol"],
                        name=item.get("name", ""),
                        alert_type="execution_pool_changed",
                        message=f"{item['symbol']} {item.get('name') or item['symbol']} 移出 execution_pool",
                    )
                )

        for case_id, curr_item in curr_candidate.items():
            prev_item = prev_candidate.get(case_id)
            if not prev_item:
                continue
            if curr_item.get("risk_gate") != prev_item.get("risk_gate"):
                events.append(
                    self._state_change_event(
                        event_at=event_at,
                        symbol=curr_item["symbol"],
                        name=curr_item.get("name", ""),
                        alert_type="risk_gate_changed",
                        message=f"{curr_item['symbol']} {curr_item.get('name') or curr_item['symbol']} risk_gate: {prev_item.get('risk_gate')} -> {curr_item.get('risk_gate')}",
                    )
                )
            if curr_item.get("audit_gate") != prev_item.get("audit_gate"):
                events.append(
                    self._state_change_event(
                        event_at=event_at,
                        symbol=curr_item["symbol"],
                        name=curr_item.get("name", ""),
                        alert_type="audit_gate_changed",
                        message=f"{curr_item['symbol']} {curr_item.get('name') or curr_item['symbol']} audit_gate: {prev_item.get('audit_gate')} -> {curr_item.get('audit_gate')}",
                    )
                )
        return events

    @staticmethod
    def _determine_notify_policy(type_counts: dict[str, int]) -> tuple[str, bool]:
        if not type_counts:
            return "none", False
        if type_counts.get("execution_pool_changed", 0) > 0:
            return "critical", True
        if type_counts.get("risk_gate_changed", 0) > 0 or type_counts.get("audit_gate_changed", 0) > 0:
            return "warning", True
        if type_counts.get("top3_changed", 0) > 0:
            return "info", False
        return "info", False

    @staticmethod
    def _format_change_line(item: dict) -> str:
        type_label = {
            "top3_changed": "前3变化",
            "execution_pool_changed": "执行池变化",
            "risk_gate_changed": "风控门变化",
            "audit_gate_changed": "审计门变化",
        }.get(item.get("alert_type"), item.get("alert_type", "状态变化"))
        return f"[{type_label}] {item.get('message', '')}"

    @staticmethod
    def _state_change_event(
        event_at: str,
        symbol: str,
        name: str,
        alert_type: str,
        message: str,
        severity: str = "info",
    ) -> dict:
        return MonitorEventRecord(
            event_id=f"monitor-event-{alert_type}-{symbol}-{event_at.replace(':', '').replace('-', '')}",
            event_at=event_at,
            event_source="state_change",
            symbol=symbol,
            name=name,
            alert_type=alert_type,
            message=message,
            severity=severity,
            price=0.0,
            change_pct=0.0,
        ).model_dump()

    def get_heartbeat_freshness(self) -> dict:
        latest = self._state_store.get("latest_heartbeat")
        if not latest:
            return {"is_fresh": False, "staleness_level": "missing", "expires_at": None}
        now = self._now_factory()
        expires_at = datetime.fromisoformat(latest["expires_at"])
        is_fresh = now <= expires_at
        return {
            "is_fresh": is_fresh,
            "staleness_level": "fresh" if is_fresh else "stale",
            "expires_at": latest["expires_at"],
        }

    def _build_items(self, snapshots: list[QuoteSnapshot], max_items: int = 20) -> list[WatchSnapshotItem]:
        ranked = sorted(
            snapshots,
            key=lambda item: (abs(self._change_pct(item)), item.volume),
            reverse=True,
        )
        items: list[WatchSnapshotItem] = []
        for snap in ranked[:max_items]:
            items.append(
                WatchSnapshotItem(
                    symbol=snap.symbol,
                    name=snap.name,
                    last_price=snap.last_price,
                    pre_close=snap.pre_close,
                    change_pct=self._change_pct(snap),
                    volume=snap.volume,
                )
            )
        return items

    @staticmethod
    def _change_pct(snapshot: QuoteSnapshot) -> float:
        if snapshot.pre_close <= 0:
            return 0.0
        return round((snapshot.last_price - snapshot.pre_close) / snapshot.pre_close, 6)

    @staticmethod
    def _resolve_phase(now: datetime) -> str:
        hhmm = now.hour * 100 + now.minute
        if 930 <= hhmm < 1000 or 1430 <= hhmm <= 1500:
            return "auction"
        return "regular"
