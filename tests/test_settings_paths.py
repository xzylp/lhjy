from __future__ import annotations

import os
import unittest
from pathlib import Path

from ashare_system.container import reset_container
from ashare_system.data.storage import build_storage_layout
from ashare_system.settings import load_settings


class SettingsPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {key: os.environ.get(key) for key in ("ASHARE_WORKSPACE", "ASHARE_STORAGE_ROOT", "ASHARE_LOGS_DIR")}
        for key in self._old_env:
            os.environ.pop(key, None)
        reset_container()

    def tearDown(self) -> None:
        reset_container()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_storage_paths_resolve_under_project_root(self) -> None:
        settings = load_settings()
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(settings.workspace, project_root)
        self.assertEqual(settings.storage_root, project_root / ".ashare_state")
        self.assertEqual(settings.logs_dir, project_root / "logs")
        layout = build_storage_layout(settings.storage_root)
        self.assertTrue(layout.raw_root.exists())
        self.assertTrue(layout.normalized_root.exists())
        self.assertTrue(layout.features_root.exists())
        self.assertTrue(layout.serving_root.exists())
        self.assertTrue(layout.raw_market_index_root.exists())
        self.assertTrue(layout.raw_events_news_root.exists())


class StorageLayoutTests(unittest.TestCase):
    def test_build_storage_layout_uses_expected_subdirectories(self) -> None:
        root = Path("/tmp/ashare-state-test")
        layout = build_storage_layout(root)
        self.assertEqual(layout.raw_market_symbol_root, root / "raw" / "market" / "symbol")
        self.assertEqual(layout.raw_market_index_root, root / "raw" / "market" / "index")
        self.assertEqual(layout.normalized_events_root, root / "normalized" / "events")
        self.assertEqual(layout.features_dossiers_root, root / "features" / "dossiers")


if __name__ == "__main__":
    unittest.main()
