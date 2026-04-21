"""JSON 原子读写工具。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    backup_path = path.with_name(f"{path.name}.bak")

    if path.exists():
        shutil.copy2(path, backup_path)

    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def read_json_with_backup(path: Path, default: Any | None = None) -> Any:
    resolved_default = {} if default is None else default
    candidates = [path, path.with_name(f"{path.name}.bak")]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
    return resolved_default
