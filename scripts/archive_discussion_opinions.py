#!/usr/bin/env python3
"""Archive discussion opinions with short local commands.

This helper exists for OpenClaw discussion workflows where dozens of opinions
must be persisted without constructing one oversized shell command.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
API_SCRIPT = ROOT_DIR / "scripts" / "ashare_api.sh"


def _load_payload(path: str) -> dict | list:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_api(method: str, path: str, body: dict | None = None) -> dict:
    command = [str(API_SCRIPT), method, path]
    if body is not None:
        command.append(json.dumps(body, ensure_ascii=False))
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"{method} {path} failed"
        raise RuntimeError(detail)
    return json.loads(completed.stdout or "{}")


def _iter_items(payload: dict | list) -> tuple[str | None, list[dict]]:
    trade_date: str | None = None
    if isinstance(payload, list):
        items = payload
    else:
        trade_date = payload.get("trade_date")
        items = payload.get("items")
        if not isinstance(items, list):
            items = payload.get("opinions")
    if not isinstance(items, list):
        raise ValueError("payload must be a list or contain items/opinions")

    round_default = payload.get("round") if isinstance(payload, dict) else None
    normalized: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("each opinion item must be an object")
        case_id = raw.get("case_id")
        if not case_id:
            raise ValueError("each opinion item requires case_id")
        opinion = {
            "round": raw.get("round", round_default),
            "agent_id": raw.get("agent_id"),
            "stance": raw.get("stance"),
            "confidence": raw.get("confidence", "medium"),
            "reasons": raw.get("reasons", []),
            "evidence_refs": raw.get("evidence_refs", []),
        }
        if not opinion["round"]:
            raise ValueError(f"missing round for case_id={case_id}")
        normalized.append({"case_id": case_id, "opinion": opinion})
    return trade_date, normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist discussion opinions with short per-case API calls.")
    parser.add_argument("payload", help="Path to a JSON payload file, or - to read from stdin.")
    parser.add_argument("--skip-refresh", action="store_true", help="Skip cycle refresh even when trade_date exists.")
    args = parser.parse_args()

    payload = _load_payload(args.payload)
    trade_date, items = _iter_items(payload)

    case_ids: list[str] = []
    results: list[dict] = []
    for item in items:
        case_id = item["case_id"]
        response = _run_api("POST", f"/system/cases/{case_id}/opinions", item["opinion"])
        if not response.get("ok"):
            raise RuntimeError(f"record opinion failed for {case_id}: {response}")
        case_ids.append(case_id)
        results.append({"case_id": case_id, "ok": True})

    rebuild_path = "/system/cases/rebuild"
    if trade_date:
        rebuild_path = f"{rebuild_path}?trade_date={trade_date}"
    rebuild = _run_api("POST", rebuild_path)
    if not rebuild.get("ok"):
        raise RuntimeError(f"rebuild failed: {rebuild}")

    refresh: dict | None = None
    if trade_date and not args.skip_refresh:
        refresh = _run_api("POST", f"/system/discussions/cycles/{trade_date}/refresh", {})
        if not refresh.get("ok"):
            raise RuntimeError(f"refresh failed: {refresh}")

    summary = {
        "ok": True,
        "trade_date": trade_date,
        "posted_count": len(items),
        "case_count": len(set(case_ids)),
        "case_ids": list(dict.fromkeys(case_ids)),
        "rebuild": {
            "ok": rebuild.get("ok", False),
            "count": rebuild.get("count", 0),
            "trade_date": rebuild.get("trade_date"),
        },
        "refresh": refresh,
        "items": results,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
