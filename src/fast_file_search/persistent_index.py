from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .utils.platform_utils import get_cache_dir

SCHEMA_VERSION = 2

_CREATE_VERSION_SQL = """
CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_MIGRATIONS: Dict[int, List[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            extension TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            modified INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL DEFAULT 0,
            is_folder INTEGER NOT NULL DEFAULT 0,
            parent_path TEXT NOT NULL DEFAULT ''
        );""",
        """CREATE TABLE IF NOT EXISTS trigrams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigram TEXT NOT NULL UNIQUE
        );""",
        """CREATE TABLE IF NOT EXISTS file_trigrams (
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            trigram_id INTEGER NOT NULL REFERENCES trigrams(id),
            PRIMARY KEY (file_id, trigram_id)
        );""",
        """CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );""",
        "CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);",
        "CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);",
        "CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension);",
        "CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_path);",
        "CREATE INDEX IF NOT EXISTS idx_trigrams_trigram ON trigrams(trigram);",
        "CREATE INDEX IF NOT EXISTS idx_ft_trigram_id ON file_trigrams(trigram_id);",
        "INSERT OR IGNORE INTO metadata (key, value) VALUES ('schema_version', '1');",
    ],
    2: [
        "ALTER TABLE files ADD COLUMN content_hash TEXT DEFAULT NULL;",
        "ALTER TABLE files ADD COLUMN content_text TEXT DEFAULT NULL;",
        "UPDATE metadata SET value = '2' WHERE key = 'schema_version';",
    ],
}


class PersistentIndex:
    def __init__(self, db_path: Optional[Path] = None):
        self._local = threading.local()
        if db_path is None:
            db_path = get_cache_dir("Fast File Search Pro") / "index.db"
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._create_tables()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA cache_size=-64000;")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    def _create_tables(self) -> None:
        conn = self._get_conn()
        conn.execute(_CREATE_VERSION_SQL)
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        current_version = row[0] if row and row[0] else 0
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            stmts = _MIGRATIONS.get(version, [])
            if not stmts:
                continue
            try:
                for stmt in stmts:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO _schema_version (version) VALUES (?)", (version,)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _generate_trigrams(self, text: str) -> List[str]:
        text = text.lower()
        if len(text) < 3:
            return [text] if text else []
        return [text[i : i + 3] for i in range(len(text) - 2)]

    def add_file(self, file_info: dict) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                path = file_info["path"]
                name = file_info.get("name", Path(path).name)
                extension = file_info.get("extension", Path(path).suffix.lower())
                size = file_info.get("size", 0)
                modified = file_info.get("modified", 0)
                created = file_info.get("created", 0)
                is_folder = file_info.get("is_folder", False)
                parent_path = file_info.get("parent_path", str(Path(path).parent))
                content_hash = file_info.get("content_hash")
                content_text = file_info.get("content_text")

                existing = conn.execute(
                    "SELECT id FROM files WHERE path = ?", (path,)
                ).fetchone()

                if existing:
                    conn.execute(
                        """UPDATE files SET name=?, extension=?, size=?,
                               modified=?, created=?, is_folder=?, parent_path=?,
                               content_hash=?, content_text=?
                           WHERE path=?""",
                        (
                            name,
                            extension,
                            size,
                            modified,
                            created,
                            is_folder,
                            parent_path,
                            content_hash,
                            content_text,
                            path,
                        ),
                    )
                    file_id = existing["id"]
                    conn.execute(
                        "DELETE FROM file_trigrams WHERE file_id = ?", (file_id,)
                    )
                else:
                    cur = conn.execute(
                        """INSERT INTO files
                               (path, name, extension, size, modified, created,
                                is_folder, parent_path, content_hash, content_text)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            path,
                            name,
                            extension,
                            size,
                            modified,
                            created,
                            is_folder,
                            parent_path,
                            content_hash,
                            content_text,
                        ),
                    )
                    file_id = cur.lastrowid

                trigrams = set(self._generate_trigrams(name))
                for gram in trigrams:
                    trig_cur = conn.execute(
                        "INSERT OR IGNORE INTO trigrams (trigram) VALUES (?)",
                        (gram,),
                    )
                    if trig_cur.lastrowid is not None:
                        trigram_id = trig_cur.lastrowid
                    else:
                        row = conn.execute(
                            "SELECT id FROM trigrams WHERE trigram = ?", (gram,)
                        ).fetchone()
                        trigram_id = row["id"] if row else None
                    if trigram_id is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO file_trigrams (file_id, trigram_id) VALUES (?, ?)",
                            (file_id, trigram_id),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def add_files_batch(self, file_infos: List[dict]) -> None:
        for i in range(0, len(file_infos), 1000):
            batch = file_infos[i : i + 1000]
            for info in batch:
                self.add_file(info)

    def remove_file(self, path: str) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM file_trigrams WHERE file_id IN (SELECT id FROM files WHERE path = ?)", (path,))
                conn.execute("DELETE FROM files WHERE path = ?", (path,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def remove_files_batch(self, paths: List[str]) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                placeholders = ",".join("?" for _ in paths)
                conn.execute(
                    f"DELETE FROM file_trigrams WHERE file_id IN (SELECT id FROM files WHERE path IN ({placeholders}))",
                    paths,
                )
                conn.execute(
                    f"DELETE FROM files WHERE path IN ({placeholders})", paths
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def rebuild_index(self) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM file_trigrams")
                conn.execute("DELETE FROM trigrams")
                conn.execute("DELETE FROM files")
                conn.execute("DELETE FROM metadata WHERE key != 'schema_version'")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_index_stats(self) -> Dict[str, object]:
        conn = self._get_conn()
        file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        folder_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE is_folder = 1"
        ).fetchone()[0]
        total_size = conn.execute(
            "SELECT COALESCE(SUM(size), 0) FROM files WHERE is_folder = 0"
        ).fetchone()[0]
        trigram_count = conn.execute("SELECT COUNT(*) FROM trigrams").fetchone()[0]
        last_updated = conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_updated'"
        ).fetchone()
        extension_dist = conn.execute(
            "SELECT extension, COUNT(*) as cnt FROM files WHERE is_folder = 0 GROUP BY extension ORDER BY cnt DESC LIMIT 20"
        ).fetchall()
        return {
            "file_count": file_count,
            "folder_count": folder_count,
            "total_size": total_size,
            "trigram_count": trigram_count,
            "last_updated": last_updated[0] if last_updated else None,
            "extension_distribution": {r["extension"]: r["cnt"] for r in extension_dist},
            "avg_file_size": total_size // max(1, file_count - folder_count),
        }

    def file_exists(self, path: str) -> bool:
        conn = self._get_conn()
        row = conn.execute("SELECT 1 FROM files WHERE path = ?", (path,)).fetchone()
        return row is not None

    def get_file_by_path(self, path: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def search_trigram(self, query: str) -> List[dict]:
        conn = self._get_conn()
        trigrams = self._generate_trigrams(query)
        if not trigrams:
            return []

        sorted_grams = sorted(
            trigrams,
            key=lambda t: conn.execute(
                "SELECT COUNT(*) FROM file_trigrams WHERE trigram_id = (SELECT id FROM trigrams WHERE trigram = ?)",
                (t,),
            ).fetchone()[0],
        )

        first_gram = sorted_grams[0]
        rows = conn.execute(
            """SELECT ft.file_id FROM trigrams t
                JOIN file_trigrams ft ON t.id = ft.trigram_id
                WHERE t.trigram = ?""",
            (first_gram,),
        ).fetchall()
        candidate_ids = {r["file_id"] for r in rows}

        for gram in sorted_grams[1:]:
            if not candidate_ids:
                break
            rows = conn.execute(
                """SELECT ft.file_id FROM trigrams t
                    JOIN file_trigrams ft ON t.id = ft.trigram_id
                    WHERE t.trigram = ?""",
                (gram,),
            ).fetchall()
            ids = {r["file_id"] for r in rows}
            candidate_ids &= ids

        query_lower = query.lower()
        results = []
        for fid in candidate_ids:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
            if row and query_lower in row["name"].lower():
                results.append(dict(row))
        return results

    def update_metadata(self, key: str, value: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()

    def get_metadata(self, key: str) -> Optional[str]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def iter_all_files(self) -> Iterator[dict]:
        conn = self._get_conn()
        cursor = conn.execute("SELECT * FROM files")
        for row in cursor:
            yield dict(row)
