"""控制面 SQLite 访问层。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import sqlite3

from .migrations import apply_control_plane_migrations


class ControlPlaneDB:
    """线程安全的轻量 SQLite 包装。"""

    def __init__(self, db_path: Path, schema_path: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.schema_path = schema_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_enabled = False
        self.ensure_initialized()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def ensure_initialized(self) -> dict[str, object]:
        with self.connect() as connection:
            connection.execute("BEGIN")
            try:
                result = apply_control_plane_migrations(connection, self.schema_path)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._fts_enabled = bool(result.get("fts5_enabled"))
        return result

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.rowcount or 0)

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]]) -> int:
        with self.connect() as connection:
            cursor = connection.executemany(sql, params_seq)
            return int(cursor.rowcount or 0)

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return None
        return next(iter(row.values()))

    def transaction(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN")
            try:
                for sql, params in statements:
                    connection.execute(sql, params)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

    def health(self) -> dict[str, Any]:
        return {
            "db_path": str(self.db_path),
            "fts5_enabled": self._fts_enabled,
            "schema_version": self.scalar("SELECT value FROM schema_meta WHERE key = ?", ("schema_version",)),
        }
