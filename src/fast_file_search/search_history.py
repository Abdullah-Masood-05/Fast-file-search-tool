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


class SearchHistory:
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_cache_dir("Fast File Search Pro") / "search_history.db"
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._create_tables()

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
        self._conn.commit()

    def add_search(self, query: str, result_count: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO search_history (query, timestamp, result_count) VALUES (?, ?, ?)",
                (query, time.time(), result_count),
            )
            self._prune(100)
            self._conn.commit()

    def _prune(self, max_entries: int = 100) -> None:
        count = self._conn.execute(
            "SELECT COUNT(*) FROM search_history"
        ).fetchone()[0]
        if count > max_entries:
            self._conn.execute(
                """DELETE FROM search_history WHERE id IN (
                    SELECT id FROM search_history ORDER BY timestamp ASC
                    LIMIT ?
                )""",
                (count - max_entries,),
            )

    def recent(self, limit: int = 20) -> List[HistoryEntry]:
        rows = self._conn.execute(
            "SELECT * FROM search_history ORDER BY timestamp DESC LIMIT ?", (limit,)
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
        rows = self._conn.execute(
            "SELECT query FROM pinned_searches ORDER BY created_at DESC"
        ).fetchall()
        return [r["query"] for r in rows]

    def frequent_queries(self, limit: int = 10) -> List[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT query, COUNT(*) as cnt FROM search_history GROUP BY query ORDER BY cnt DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r["query"], r["cnt"]) for r in rows]

    def close(self) -> None:
        self._conn.close()


class Autocomplete:
    def __init__(self, persistent_index: Optional[PersistentIndex] = None):
        self.index = persistent_index or PersistentIndex()
        self._freq_cache: Dict[str, int] = {}
        self._last_build: float = 0
        self._cache_ttl: float = 60.0
        self._lock = threading.RLock()

    def _build_freq_cache(self) -> None:
        now = time.time()
        if now - self._last_build < self._cache_ttl and self._freq_cache:
            return
        freq: Counter = Counter()
        for f in self.index.iter_all_files():
            name = f.get("name", "")
            if name:
                freq[name] += 1
        self._freq_cache = dict(freq)
        self._last_build = now

    def suggest(self, prefix: str, limit: int = 10, context_path: Optional[str] = None) -> List[str]:
        if not prefix:
            return []
        self._build_freq_cache()
        prefix_lower = prefix.lower()
        seen: Set[str] = set()
        scored: List[tuple[str, int, int]] = []

        for name, freq in self._freq_cache.items():
            if name.lower().startswith(prefix_lower) and name not in seen:
                seen.add(name)
                scored.append((name, freq, 0))
            elif prefix_lower in name.lower() and name not in seen:
                seen.add(name)
                scored.append((name, freq, 1))

        scored.sort(key=lambda x: (x[2], -x[1], x[0]))
        return [s[0] for s in scored[:limit]]

    def context_suggest(self, prefix: str, current_path: str, limit: int = 10) -> List[str]:
        if not prefix:
            return []
        self._build_freq_cache()
        prefix_lower = prefix.lower()
        path_lower = current_path.lower() if current_path else ""
        seen: Set[str] = set()
        scored: List[tuple[str, int, bool]] = []

        for f in self.index.iter_all_files():
            name = f.get("name", "")
            parent = f.get("parent_path", "")
            if not name or name in seen:
                continue
            if not (name.lower().startswith(prefix_lower) or prefix_lower in name.lower()):
                continue
            is_context = path_lower and path_lower in parent.lower()
            seen.add(name)
            freq = self._freq_cache.get(name, 0)
            scored.append((name, freq, is_context))

        scored.sort(key=lambda x: (-x[2], -x[1], x[0]))
        return [s[0] for s in scored[:limit]]


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
        for f in self.index.iter_all_files():
            ext = f.get("extension", "")
            if ext:
                ext_count[ext] += 1
        self._ext_cache = ext_count.most_common(20)
        self._last_build = now

    def did_you_mean(self, term: str, max_distance: int = 2) -> Optional[str]:
        if not term:
            return None
        all_names: Set[str] = set()
        for f in self.index.iter_all_files():
            name = f.get("name", "")
            if name:
                all_names.add(name.lower())
                if len(all_names) >= 3000:
                    break

        best: Optional[tuple[str, int]] = None
        term_lower = term.lower()
        for name in all_names:
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
        for f in self.index.iter_all_files():
            name = f.get("name", "").lower()
            name_tri = set()
            for i in range(len(name) - 2):
                name_tri.add(name[i : i + 3])
            overlap = len(trigrams & name_tri)
            if overlap > 0 and name != term_lower:
                shared[f.get("name", "")] += overlap

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
