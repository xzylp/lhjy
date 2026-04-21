from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ashare_system.infra.safe_json import atomic_write_json, read_json_with_backup


class SafeJsonTests(unittest.TestCase):
    def test_atomic_write_json_creates_backup_and_can_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "state.json"
            atomic_write_json(path, {"value": 1})
            atomic_write_json(path, {"value": 2})
            backup_path = path.with_name("state.json.bak")
            self.assertTrue(backup_path.exists())
            self.assertEqual(read_json_with_backup(path), {"value": 2})

            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(read_json_with_backup(path), {"value": 1})

    def test_read_json_with_backup_returns_default_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "missing.json"
            payload = read_json_with_backup(path, default={"items": []})
            self.assertEqual(payload, {"items": []})


if __name__ == "__main__":
    unittest.main()
