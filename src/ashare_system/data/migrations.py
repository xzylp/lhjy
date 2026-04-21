"""控制面 SQLite schema 初始化与迁移。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3


CURRENT_SCHEMA_VERSION = "2026-04-21-r1"


def load_schema_sql(schema_path: Path | None = None) -> str:
    resolved = schema_path or Path(__file__).with_name("schema.sql")
    return resolved.read_text(encoding="utf-8")


def _utc_now() -> str:
    return datetime.now().isoformat()


def apply_control_plane_migrations(connection: sqlite3.Connection, schema_path: Path | None = None) -> dict[str, object]:
    connection.executescript(load_schema_sql(schema_path))
    connection.executescript(
        """
        DROP TRIGGER IF EXISTS documents_ai;
        DROP TRIGGER IF EXISTS documents_ad;
        DROP TRIGGER IF EXISTS documents_au;
        """
    )
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO schema_meta(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        ("schema_version", CURRENT_SCHEMA_VERSION, now),
    )
    connection.execute(
        """
        INSERT INTO schema_meta(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        ("initialized_at", now, now),
    )

    fts_enabled = False
    try:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
            USING fts5(doc_id UNINDEXED, title, summary, content, tokenize='unicode61');
            """
        )
        fts_enabled = True
        connection.execute(
            """
            INSERT INTO schema_meta(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            ("fts5_enabled", "true", now),
        )
    except sqlite3.OperationalError:
        connection.execute(
            """
            INSERT INTO schema_meta(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            ("fts5_enabled", "false", now),
        )
    return {"schema_version": CURRENT_SCHEMA_VERSION, "fts5_enabled": fts_enabled}
