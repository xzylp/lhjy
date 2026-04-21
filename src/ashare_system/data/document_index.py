"""项目文档全文检索。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
from typing import Any

from .catalog_service import CatalogService
from .control_db import ControlPlaneDB


def _now() -> str:
    return datetime.now().isoformat()


def _normalize_summary(text: str) -> tuple[str, str]:
    title = ""
    summary = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not title and line.startswith("#"):
            title = line.lstrip("#").strip()
            continue
        if not title:
            title = line[:120]
        if not summary:
            summary = line[:240]
        if title and summary:
            break
    return title or "未命名文档", summary


class DocumentIndexService:
    """把仓库文档沉到 SQLite，并提供 FTS 查询。"""

    def __init__(self, db: ControlPlaneDB, catalog_service: CatalogService | None = None) -> None:
        self.db = db
        self.catalog_service = catalog_service
        if self.catalog_service is not None:
            self.catalog_service.ensure_default_catalog()

    def upsert_document(
        self,
        *,
        doc_id: str,
        title: str,
        content: str,
        category: str = "general",
        path: str = "",
        summary: str = "",
        source: str = "workspace",
        trade_date: str = "",
        metadata: dict[str, Any] | None = None,
        refresh_fts: bool = True,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO documents(doc_id, category, title, path, summary, content, source, trade_date, updated_at, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                category=excluded.category,
                title=excluded.title,
                path=excluded.path,
                summary=excluded.summary,
                content=excluded.content,
                source=excluded.source,
                trade_date=excluded.trade_date,
                updated_at=excluded.updated_at,
                metadata_json=excluded.metadata_json
            """,
            (
                doc_id,
                category,
                title,
                path,
                summary,
                content,
                source,
                trade_date,
                _now(),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        if refresh_fts:
            self.rebuild_fts_index()

    def index_markdown_file(self, path: Path, *, category: str = "docs", source: str = "workspace") -> bool:
        if not path.exists() or not path.is_file():
            return False
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return False
        title, summary = _normalize_summary(content)
        trade_date = ""
        for token in reversed(path.stem.split("_")):
            if len(token) == 8 and token.isdigit():
                trade_date = f"{token[:4]}-{token[4:6]}-{token[6:]}"
                break
        self.upsert_document(
            doc_id=str(path.resolve()),
            title=title,
            content=content,
            category=category,
            path=str(path),
            summary=summary,
            source=source,
            trade_date=trade_date,
            metadata={"suffix": path.suffix, "name": path.name},
            refresh_fts=False,
        )
        return True

    def index_workspace_documents(self, workspace: Path) -> dict[str, Any]:
        patterns = [
            "README.md",
            "walkthrough.md",
            "task*.md",
            "docs/*.md",
        ]
        indexed = 0
        seen: set[Path] = set()
        for pattern in patterns:
            for path in workspace.glob(pattern):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                if self.index_markdown_file(path):
                    indexed += 1
        self.rebuild_fts_index()
        return {"indexed_count": indexed, "fts5_enabled": self.db.fts_enabled}

    def rebuild_fts_index(self) -> None:
        if not self.db.fts_enabled:
            return
        with self.db.connect() as connection:
            connection.execute("DROP TABLE IF EXISTS documents_fts")
            connection.execute(
                "CREATE VIRTUAL TABLE documents_fts USING fts5(doc_id UNINDEXED, title, summary, content, tokenize='unicode61')"
            )
            connection.execute(
                """
                INSERT INTO documents_fts(rowid, doc_id, title, summary, content)
                SELECT rowid, doc_id, title, summary, content
                FROM documents
                """
            )

    def search(self, query: str, *, limit: int = 10, category: str | None = None) -> list[dict[str, Any]]:
        normalized = str(query or "").strip()
        if not normalized:
            sql = "SELECT doc_id, category, title, path, summary, trade_date, updated_at FROM documents"
            params: list[Any] = []
            if category:
                sql += " WHERE category = ?"
                params.append(category)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            return self.db.query_all(sql, tuple(params))

        if self.db.fts_enabled:
            sql = """
                SELECT d.doc_id, d.category, d.title, d.path, d.summary, d.trade_date, d.updated_at
                FROM documents_fts f
                JOIN documents d ON d.rowid = f.rowid
                WHERE documents_fts MATCH ?
            """
            params: list[Any] = [normalized]
            if category:
                sql += " AND d.category = ?"
                params.append(category)
            sql += " ORDER BY d.updated_at DESC LIMIT ?"
            params.append(limit)
            fts_items = self.db.query_all(sql, tuple(params))
            if fts_items:
                return fts_items

        like = f"%{normalized}%"
        sql = """
            SELECT doc_id, category, title, path, summary, trade_date, updated_at
            FROM documents
            WHERE (title LIKE ? OR summary LIKE ? OR content LIKE ?)
        """
        params = [like, like, like]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return self.db.query_all(sql, tuple(params))

    def stats(self) -> dict[str, Any]:
        return {
            "document_count": int(self.db.scalar("SELECT COUNT(*) FROM documents") or 0),
            "fts5_enabled": self.db.fts_enabled,
        }
