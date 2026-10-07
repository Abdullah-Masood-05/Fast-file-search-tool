from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .persistent_index import PersistentIndex
from .search import Levenshtein
from .utils.platform_utils import get_cache_dir


@dataclass
class HistoryEntry:
    query: str
    timestamp: float
    result_count: int = 0
    pinned: bool = False
    id: Optional[int] = None


def _like_escape(text: str) -> str:
    """Escape LIKE wildcards so user input is matched literally (use with ESCAPE '!')."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


class SearchHistory:
    MAX_ENTRIES = 100

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_cache_dir("Fast File Search Pro") / "search_history.db"
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn = self._open_with_recovery()

    @staticmethod
    def _connect(target: str) -> sqlite3.Connection:
        conn = sqlite3.connect(target, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
            except sqlite3.OperationalError:
                pass  # e.g. read-only media; the default journal mode still works
        except BaseException:
            conn.close()  # release the file handle (a corrupt file must be movable on Windows)
            raise
        return conn

    def _try_open(self) -> sqlite3.Connection:
        conn = self._connect(str(self.db_path))
        try:
            self._conn = conn
            self._create_tables()
        except BaseException:
            conn.close()
            raise
        return conn

    def _quarantine(self) -> None:
        backup = self.db_path.with_name(self.db_path.name + ".corrupt")
        try:
            if backup.exists():
                backup.unlink()
            self.db_path.replace(backup)
        except OSError:
            try:
                self.db_path.unlink()
            except OSError:
                pass
        for suffix in ("-wal", "-shm"):
            side = self.db_path.with_name(self.db_path.name + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError:
                pass

    def _open_with_recovery(self) -> sqlite3.Connection:
        """Open the history DB; move a corrupt file aside, or fall back to memory."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                return self._try_open()
            except sqlite3.DatabaseError:
                self._quarantine()
                return self._try_open()
        except (sqlite3.Error, OSError):
            # History is non-essential: degrade to an in-memory store instead of crashing.
            conn = self._connect(":memory:")
            self._conn = conn
            self._create_tables()
            return conn

    def _create_tables(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                timestamp REAL NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0
            );"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_query ON search_history(query);"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_ts ON search_history(timestamp);"
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS pinned_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL
            );"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS favorites (
                path TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            );"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS tags (
                path TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (path, tag)
            );"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS notes (
                path TEXT PRIMARY KEY,
                note TEXT NOT NULL
            );"""
        )
        self._conn.commit()

    def _read_all(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _read_one(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def add_search(self, query: str, result_count: int = 0) -> None:
        query = (query or "").strip()
        if not query:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO search_history (query, timestamp, result_count) VALUES (?, ?, ?)",
                (query, time.time(), result_count),
            )
            self._prune(self.MAX_ENTRIES)
            self._conn.commit()

    def _prune(self, max_entries: int = 100) -> None:
        # ids only ever increase, so they give a stable newest-first order even when two
        # searches share one timestamp (coarse clocks on Windows).
        self._conn.execute(
            """DELETE FROM search_history WHERE id NOT IN (
                SELECT id FROM search_history ORDER BY id DESC LIMIT ?
            )""",
            (max(1, max_entries),),
        )

    def recent(self, limit: int = 20) -> List[HistoryEntry]:
        """Most recent distinct queries; repeats collapse into their latest run."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT h.id, h.query, h.timestamp, h.result_count,
                          EXISTS(SELECT 1 FROM pinned_searches p WHERE p.query = h.query) AS pinned
                   FROM search_history h
                   WHERE h.id IN (SELECT MAX(id) FROM search_history GROUP BY query)
                   ORDER BY h.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            HistoryEntry(
                id=r["id"],
                query=r["query"],
                timestamp=r["timestamp"],
                result_count=r["result_count"],
                pinned=bool(r["pinned"]),
            )
            for r in rows
        ]

    def clear_history(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM search_history")
            self._conn.commit()

    def pin_search(self, query: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pinned_searches (query, created_at) VALUES (?, ?)",
                (query, time.time()),
            )
            self._conn.commit()

    def unpin_search(self, query: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM pinned_searches WHERE query = ?", (query,)
            )
            self._conn.commit()

    def pinned_searches(self) -> List[str]:
        rows = self._read_all(
            "SELECT query FROM pinned_searches ORDER BY created_at DESC"
        )
        return [r["query"] for r in rows]

    def frequent_queries(self, limit: int = 10) -> List[tuple[str, int]]:
        rows = self._read_all(
            "SELECT query, COUNT(*) as cnt FROM search_history GROUP BY query ORDER BY cnt DESC LIMIT ?",
            (limit,),
        )
        return [(r["query"], r["cnt"]) for r in rows]

    def add_favorite(self, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO favorites (path, created_at) VALUES (?, ?)",
                (path, time.time()),
            )
            self._conn.commit()

    def remove_favorite(self, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM favorites WHERE path = ?", (path,)
            )
            self._conn.commit()

    def is_favorite(self, path: str) -> bool:
        row = self._read_one(
            "SELECT 1 FROM favorites WHERE path = ?", (path,)
        )
        return row is not None

    def get_favorites(self) -> List[str]:
        rows = self._read_all(
            "SELECT path FROM favorites ORDER BY created_at DESC"
        )
        return [r["path"] for r in rows]

    def add_tag(self, path: str, tag: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO tags (path, tag) VALUES (?, ?)",
                (path, tag),
            )
            self._conn.commit()

    def remove_tag(self, path: str, tag: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM tags WHERE path = ? AND tag = ?", (path, tag)
            )
            self._conn.commit()

    def get_tags(self, path: str) -> List[str]:
        rows = self._read_all(
            "SELECT tag FROM tags WHERE path = ? ORDER BY tag ASC", (path,)
        )
        return [r["tag"] for r in rows]

    def set_note(self, path: str, note: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO notes (path, note) VALUES (?, ?)",
                (path, note),
            )
            self._conn.commit()

    def get_note(self, path: str) -> str:
        row = self._read_one(
            "SELECT note FROM notes WHERE path = ?", (path,)
        )
        return row["note"] if row else ""

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


class Autocomplete:
    def __init__(self, persistent_index: Optional[PersistentIndex] = None):
        self.index = persistent_index or PersistentIndex()

    def suggest(self, prefix: str, limit: int = 8) -> List[str]:
        """Fast SQL-backed autocomplete suggestions."""
        p_clean = prefix.strip().lower()
        if not p_clean:
            return []
        esc = _like_escape(p_clean)

        try:
            conn = self.index._get_conn()
            # 1. Prefix matches first
            rows = conn.execute(
                "SELECT DISTINCT name FROM files WHERE LOWER(name) LIKE ? ESCAPE '!' LIMIT ?",
                (f"{esc}%", limit),
            ).fetchall()
            results = [r["name"] for r in rows]

            # 2. Substring matches if limit not reached
            if len(results) < limit:
                needed = limit - len(results)
                c_rows = conn.execute(
                    "SELECT DISTINCT name FROM files WHERE LOWER(name) LIKE ? ESCAPE '!' "
                    "AND LOWER(name) NOT LIKE ? ESCAPE '!' LIMIT ?",
                    (f"%{esc}%", f"{esc}%", needed),
                ).fetchall()
                results.extend([r["name"] for r in c_rows])

            return results
        except Exception:
            return []

    def context_suggest(self, prefix: str, current_path: str, limit: int = 10) -> List[str]:
        """Suggest names containing ``prefix``, preferring files under ``current_path``."""
        p_clean = (prefix or "").strip().lower()
        if not p_clean:
            return []
        ctx = current_path.lower().replace(chr(92), "/") if current_path else ""
        try:
            conn = self.index._get_conn()
            rows = conn.execute(
                """SELECT name, COUNT(*) AS cnt,
                          MAX(CASE WHEN ? != '' AND REPLACE(LOWER(parent_path), char(92), '/')
                                        LIKE ? ESCAPE '!' THEN 1 ELSE 0 END) AS ctx
                   FROM files WHERE LOWER(name) LIKE ? ESCAPE '!'
                   GROUP BY name
                   ORDER BY ctx DESC,
                            CASE WHEN LOWER(name) LIKE ? ESCAPE '!' THEN 0 ELSE 1 END,
                            cnt DESC, name
                   LIMIT ?""",
                (
                    ctx,
                    f"%{_like_escape(ctx)}%",
                    f"%{_like_escape(p_clean)}%",
                    f"{_like_escape(p_clean)}%",
                    limit,
                ),
            ).fetchall()
            return [r["name"] for r in rows]
        except Exception:
            return []


class SearchRecommender:
    def __init__(self, persistent_index: Optional[PersistentIndex] = None):
        self.index = persistent_index or PersistentIndex()
        self._ext_cache: List[tuple[str, int]] = []
        self._last_build: float = 0
        self._cache_ttl: float = 120.0

    def _refresh(self) -> None:
        now = time.time()
        if now - self._last_build < self._cache_ttl and self._ext_cache:
            return
        ext_count: Counter = Counter()
        try:
            rows = self.index._get_conn().execute(
                "SELECT extension, COUNT(*) AS cnt FROM files "
                "WHERE extension != '' GROUP BY extension ORDER BY cnt DESC LIMIT 20"
            ).fetchall()
            for r in rows:
                ext_count[r["extension"]] = r["cnt"]
        except Exception:
            pass
        self._ext_cache = ext_count.most_common(20)
        self._last_build = now

    def _sample_names(self, limit: int) -> List[str]:
        """Distinct file names without dragging every column (e.g. content_text) into memory."""
        try:
            rows = self.index._get_conn().execute(
                "SELECT DISTINCT name FROM files LIMIT ?", (limit,)
            ).fetchall()
            return [r["name"] for r in rows if r["name"]]
        except Exception:
            return []

    def did_you_mean(self, term: str, max_distance: int = 2) -> Optional[str]:
        if not term:
            return None
        all_names: Set[str] = {n.lower() for n in self._sample_names(3000)}

        best: Optional[tuple[str, int]] = None
        term_lower = term.lower()
        for name in sorted(all_names):
            d = Levenshtein.distance(term_lower, Path(name).stem)
            if 0 < d <= max_distance:
                if best is None or d < best[1] or (d == best[1] and len(name) < len(best[0])):
                    best = (name, d)

        return best[0] if best else None

    def related_searches(self, term: str, limit: int = 5) -> List[str]:
        if not term:
            return []
        term_lower = term.lower()
        trigrams = set()
        for i in range(len(term_lower) - 2):
            trigrams.add(term_lower[i : i + 3])

        shared: Dict[str, int] = defaultdict(int)
        for original in self._sample_names(20000):
            name = original.lower()
            name_tri = set()
            for i in range(len(name) - 2):
                name_tri.add(name[i : i + 3])
            overlap = len(trigrams & name_tri)
            if overlap > 0 and name != term_lower:
                shared[original] += overlap

        sorted_shared = sorted(shared.items(), key=lambda x: -x[1])
        return [s[0] for s in sorted_shared[:limit]]

    def popular_file_types(self, limit: int = 10) -> List[tuple[str, int]]:
        self._refresh()
        return self._ext_cache[:limit]

    def recommendations(self, query: str, limit: int = 5) -> List[dict]:
        results: List[dict] = []

        dym = self.did_you_mean(query)
        if dym:
            results.append({"type": "did_you_mean", "value": dym})

        related = self.related_searches(query, limit=3)
        for r in related:
            results.append({"type": "related", "value": r})

        types = self.popular_file_types(3)
        for ext, cnt in types:
            results.append({"type": "popular_type", "value": ext, "count": cnt})

        return results[:limit]
