from __future__ import annotations

from datetime import datetime
import unittest

from ashare_system.data.freshness import build_freshness_meta, classify_staleness


class FreshnessTests(unittest.TestCase):
    def test_classify_staleness_distinguishes_fresh_warm_stale(self) -> None:
        now = datetime.fromisoformat("2026-04-05T10:00:00+08:00")
        self.assertEqual(
            classify_staleness(
                "2026-04-05T09:59:50+08:00",
                now=now,
                fresh_seconds=30,
                warm_seconds=300,
            ),
            "fresh",
        )
        self.assertEqual(
            classify_staleness(
                "2026-04-05T09:58:00+08:00",
                now=now,
                fresh_seconds=30,
                warm_seconds=300,
            ),
            "warm",
        )
        self.assertEqual(
            classify_staleness(
                "2026-04-05T09:40:00+08:00",
                now=now,
                fresh_seconds=30,
                warm_seconds=300,
            ),
            "stale",
        )

    def test_build_freshness_meta_sets_expiry_and_staleness(self) -> None:
        now = datetime.fromisoformat("2026-04-05T10:00:00+08:00")
        meta = build_freshness_meta(
            source_at="2026-04-05T09:59:40+08:00",
            fetched_at="2026-04-05T09:59:45+08:00",
            generated_at="2026-04-05T09:59:50+08:00",
            fresh_seconds=30,
            expiry_seconds=60,
            now=now,
        )
        self.assertEqual(meta.staleness_level, "fresh")
        self.assertEqual(meta.expires_at, "2026-04-05T10:00:40+08:00")


if __name__ == "__main__":
    unittest.main()
