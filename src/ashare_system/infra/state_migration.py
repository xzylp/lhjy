"""状态文件拆分迁移。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .audit_store import _atomic_write_json, _file_lock, _load_json_payload

DISCUSSION_STATE_KEYS = {
    "latest_discussion_context",
}

DISCUSSION_STATE_PREFIXES = (
    "discussion_context:",
)

EXECUTION_GATEWAY_STATE_KEYS = {
    "pending_execution_intents",
    "latest_execution_gateway_receipt",
    "execution_gateway_receipt_history",
    "execution_intent_history",
    "latest_execution_intents",
}


def migrate_legacy_state_files(storage_root: Path) -> dict[str, Any]:
    """把历史 meeting_state 中的大块 discussion/gateway 状态迁移到独立文件。"""

    root = Path(storage_root)
    meeting_path = root / "meeting_state.json"
    discussion_path = root / "discussion_state.json"
    execution_gateway_path = root / "execution_gateway_state.json"

    migrated_discussion_keys: list[str] = []
    migrated_gateway_keys: list[str] = []

    with _file_lock(meeting_path):
        meeting_payload = _load_json_payload(meeting_path, {})
        if not isinstance(meeting_payload, dict):
            meeting_payload = {}

        discussion_payload = _load_json_payload(discussion_path, {})
        if not isinstance(discussion_payload, dict):
            discussion_payload = {}

        gateway_payload = _load_json_payload(execution_gateway_path, {})
        if not isinstance(gateway_payload, dict):
            gateway_payload = {}

        for key in list(meeting_payload.keys()):
            if key in DISCUSSION_STATE_KEYS or key.startswith(DISCUSSION_STATE_PREFIXES):
                discussion_payload[key] = meeting_payload.pop(key)
                migrated_discussion_keys.append(key)
                continue
            if key in EXECUTION_GATEWAY_STATE_KEYS:
                gateway_payload[key] = meeting_payload.pop(key)
                migrated_gateway_keys.append(key)

        if migrated_discussion_keys:
            _atomic_write_json(discussion_path, discussion_payload)
        if migrated_gateway_keys:
            _atomic_write_json(execution_gateway_path, gateway_payload)
        if migrated_discussion_keys or migrated_gateway_keys:
            _atomic_write_json(meeting_path, meeting_payload)

    return {
        "ok": True,
        "meeting_path": str(meeting_path),
        "discussion_path": str(discussion_path),
        "execution_gateway_path": str(execution_gateway_path),
        "migrated_discussion_count": len(migrated_discussion_keys),
        "migrated_gateway_count": len(migrated_gateway_keys),
        "migrated_discussion_keys": migrated_discussion_keys,
        "migrated_gateway_keys": migrated_gateway_keys,
    }
