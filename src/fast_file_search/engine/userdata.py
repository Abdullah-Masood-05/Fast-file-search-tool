"""User data: search history, pinned searches, favorites, tags and notes (design A.8).

A port of v1's ``SearchHistory``: same file (``search_history.db``), same tables and
semantics, plus ``PRAGMA user_version = 1``. A v1 file is used as is (nothing to migrate but
the version stamp). Only the engine process opens it.

Robustness: a corrupt file is moved to ``search_history.db.corrupt`` and the newest daily
backup (``.bak``, then ``.bak.1``) is restored; if that fails a fresh file is created; if the
location is unusable everything runs in memory. ``recovered`` says which of these happened
('restored' | 'reset' | 'memory' | None) so the service can send a ``Notice``.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional

from .store import decode_db_text, encode_db_text, looks_corrupt
from .textfold import fold

log = logging.getLogger(__name__)

USER_VERSION = 1
DB_NAME = "search_history.db"


class HistoryItem(NamedTuple):
    query: str
    count: int           # result count of the latest run (0 for a pin never run)
    pinned: bool
    last_used: float     # epoch seconds


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        timestamp REAL NOT NULL,
        result_count INTEGER NOT NULL DEFAULT 0,
        pinned INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_history_query ON search_history(query)",
    "CREATE INDEX IF NOT EXISTS idx_history_ts ON search_history(timestamp)",
    """CREATE TABLE IF NOT EXISTS pinned_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS favorites (path TEXT PRIMARY KEY, created_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS tags (path TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (path, tag))",
    "CREATE TABLE IF NOT EXISTS notes (path TEXT PRIMARY KEY, note TEXT NOT NULL)",
)


def matches_prefix(query: str, typed: str) -> bool:
    """History matching rule (E.1): folded prefix, or substring once 3+ chars are typed."""
    if not typed:
        return True
    fq, ft = fold(query), fold(typed)
    return fq.startswith(ft) or (len(ft) >= 3 and ft in fq)


class UserData:
    MAX_ENTRIES = 500          # history rows kept (repeats included)
    RECENT = 200               # distinct recent queries held in memory for suggestions
    BACKUP_EVERY = 86_400.0
    BACKUPS = 2

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self.recovered: Optional[str] = None
        self.favorites_generation = 0
        self._favs: Optional[frozenset] = None
        self._recent: List[HistoryItem] = []
        self._pins: List[str] = []
        self._conn = self._open_with_recovery()
        self._load_caches()

    # ------------------------------------------------------------------ open / recovery
    @staticmethod
    def _connect(target: str) -> sqlite3.Connection:
        conn = sqlite3.connect(target, timeout=30, check_same_thread=False)
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass  # read-only media: the default journal still works
        except BaseException:
            conn.close()
            raise
        return conn

    def _init(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("PRAGMA quick_check(1)").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise sqlite3.DatabaseError(f"database disk image is malformed ({row[0] if row else '?'})")
        for stmt in _SCHEMA:
            conn.execute(stmt)
        if conn.execute("PRAGMA user_version").fetchone()[0] < USER_VERSION:
            conn.execute(f"PRAGMA user_version={USER_VERSION}")
        conn.commit()

    def _try_open(self) -> sqlite3.Connection:
        conn = self._connect(str(self.path))
        try:
            self._init(conn)
        except BaseException:
            conn.close()   # release the handle: Windows cannot move an open file
            raise
        return conn

    def _side_files(self) -> List[Path]:
        return [self.path.with_name(self.path.name + s) for s in ("-wal", "-shm")]

    def _quarantine(self) -> None:
        target = self.path.with_name(self.path.name + ".corrupt")
        try:
            if target.exists():
                target.unlink()
            if self.path.exists():
                os.replace(self.path, target)
        except OSError:
            try:
                self.path.unlink()
            except OSError:
                pass
        for side in self._side_files():
            try:
                side.unlink()
            except OSError:
                pass

    def _backups(self) -> List[Path]:
        return [self.path.with_name(self.path.name + (".bak" if i == 0 else f".bak.{i}")) for i in range(self.BACKUPS)]

    def _open_with_recovery(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                return self._try_open()
            except sqlite3.DatabaseError as exc:
                log.warning("user data %s unusable (%s); recovering", self.path.name, exc)
                self._quarantine()
                for bak in self._backups():
                    if not bak.exists():
                        continue
                    try:
                        shutil.copyfile(bak, self.path)
                        conn = self._try_open()
                        self.recovered = "restored"
                        log.warning("user data restored from %s", bak.name)
                        return conn
                    except (sqlite3.Error, OSError) as exc2:
                        log.warning("backup %s unusable: %s", bak.name, exc2)
                        self._quarantine()
                self.recovered = "reset"
                return self._try_open()
        except (sqlite3.Error, OSError) as exc:
            # User data is non-essential: degrade to memory instead of failing the engine.
            log.warning("user data kept in memory only: %s", exc)
            self.recovered = "memory"
            conn = self._connect(":memory:")
            self._init(conn)
            return conn

    def _run(self, fn: Callable[[sqlite3.Connection], object]):
        """Run a write; a corrupt file mid-session is recovered and the write retried once."""
        with self._lock:
            try:
                out = fn(self._conn)
                self._conn.commit()
                return out
            except sqlite3.DatabaseError as exc:
                if not looks_corrupt(exc):
                    raise
                log.warning("user data damaged during use (%s); recovering", exc)
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = self._open_with_recovery()
                self._load_caches()
                out = fn(self._conn)
                self._conn.commit()
                return out

    def _load_caches(self) -> None:
        with self._lock:
            c = self._conn
            self._pins = [r[0] for r in c.execute(
                "SELECT query FROM pinned_searches ORDER BY created_at DESC, id DESC")]
            rows = c.execute(
                "SELECT h.query, h.result_count, h.timestamp FROM search_history h "
                "WHERE h.id IN (SELECT MAX(id) FROM search_history GROUP BY query) "
                "ORDER BY h.id DESC LIMIT ?", (self.RECENT,)).fetchall()
            pins = set(self._pins)
            self._recent = [HistoryItem(q, n, q in pins, t) for q, n, t in rows]
            self._favs = None

    # ------------------------------------------------------------------ history
    def record_search(self, text: str, result_count: int) -> None:
        q = (text or "").strip()
        if not q:
            return
        now = self.clock()

        def write(c: sqlite3.Connection) -> None:
            c.execute("INSERT INTO search_history (query, timestamp, result_count) VALUES (?, ?, ?)",
                      (q, now, int(result_count)))
            # ids only increase, so they order runs newest-first even with equal timestamps
            c.execute("DELETE FROM search_history WHERE id NOT IN "
                      "(SELECT id FROM search_history ORDER BY id DESC LIMIT ?)", (max(1, self.MAX_ENTRIES),))
        self._run(write)
        with self._lock:
            self._recent = [h for h in self._recent if h.query != q]
            self._recent.insert(0, HistoryItem(q, int(result_count), q in self._pins, now))
            del self._recent[self.RECENT:]

    def history(self, prefix: str = "", limit: int = 20) -> List[HistoryItem]:
        """Pinned searches first (newest pin first), then recent distinct queries (newest
        first), filtered by ``matches_prefix``."""
        typed = (prefix or "").strip()
        with self._lock:
            latest: Dict[str, HistoryItem] = {h.query: h for h in self._recent}
            out: List[HistoryItem] = []
            for q in self._pins:
                if matches_prefix(q, typed):
                    h = latest.get(q)
                    out.append(HistoryItem(q, h.count if h else 0, True, h.last_used if h else 0.0))
            pins = set(self._pins)
            out.extend(h for h in self._recent if h.query not in pins and matches_prefix(h.query, typed))
            return out[:max(0, limit)]

    def pin(self, text: str) -> None:
        q = (text or "").strip()
        if not q:
            return
        now = self.clock()
        self._run(lambda c: c.execute(
            "INSERT OR REPLACE INTO pinned_searches (query, created_at) VALUES (?, ?)", (q, now)))
        with self._lock:
            self._pins = [q] + [p for p in self._pins if p != q]
            self._recent = [h._replace(pinned=h.query in self._pins) for h in self._recent]

    def unpin(self, text: str) -> None:
        q = (text or "").strip()
        self._run(lambda c: c.execute("DELETE FROM pinned_searches WHERE query = ?", (q,)))
        with self._lock:
            self._pins = [p for p in self._pins if p != q]
            self._recent = [h._replace(pinned=h.query in self._pins) for h in self._recent]

    def pinned(self) -> List[str]:
        with self._lock:
            return list(self._pins)

    def clear_history(self) -> None:
        """Remove every history row; pinned searches stay."""
        self._run(lambda c: c.execute("DELETE FROM search_history"))
        with self._lock:
            self._recent = []

    def frequent_queries(self, limit: int = 10) -> List[tuple]:
        with self._lock:
            return [tuple(r) for r in self._conn.execute(
                "SELECT query, COUNT(*) AS cnt FROM search_history GROUP BY query ORDER BY cnt DESC, MAX(id) DESC "
                "LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ favorites
    def set_favorite(self, path: str, on: bool) -> None:
        enc = encode_db_text(path)
        now = self.clock()
        if on:
            self._run(lambda c: c.execute("INSERT OR REPLACE INTO favorites (path, created_at) VALUES (?, ?)",
                                          (enc, now)))
        else:
            self._run(lambda c: c.execute("DELETE FROM favorites WHERE path = ?", (enc,)))
        with self._lock:
            self._favs = None
            self.favorites_generation += 1

    def is_favorite(self, path: str) -> bool:
        return path in self.favorite_paths()

    def favorite_paths(self) -> frozenset:
        with self._lock:
            if self._favs is None:
                self._favs = frozenset(decode_db_text(r[0]) for r in self._conn.execute("SELECT path FROM favorites"))
            return self._favs

    def favorites(self) -> List[str]:
        """Favorite paths, newest first."""
        with self._lock:
            return [decode_db_text(r[0]) for r in self._conn.execute(
                "SELECT path FROM favorites ORDER BY created_at DESC")]

    # ------------------------------------------------------------------ tags and notes
    def tags(self, path: str) -> List[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT tag FROM tags WHERE path = ? ORDER BY tag ASC", (encode_db_text(path),))]

    def set_tags(self, path: str, tags: Iterable[str]) -> None:
        enc = encode_db_text(path)
        clean = sorted({t.strip() for t in tags if t and t.strip()})

        def write(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM tags WHERE path = ?", (enc,))
            c.executemany("INSERT OR IGNORE INTO tags (path, tag) VALUES (?, ?)", [(enc, t) for t in clean])
        self._run(write)

    def note(self, path: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT note FROM notes WHERE path = ?", (encode_db_text(path),)).fetchone()
        return row[0] if row else ""

    def set_note(self, path: str, note: str) -> None:
        enc = encode_db_text(path)
        text = note or ""
        if text.strip():
            self._run(lambda c: c.execute("INSERT OR REPLACE INTO notes (path, note) VALUES (?, ?)", (enc, text)))
        else:
            self._run(lambda c: c.execute("DELETE FROM notes WHERE path = ?", (enc,)))

    def forget(self, paths: Iterable[str]) -> None:
        """Called after files were trashed or deleted. Favorites, tags and notes are kept on
        purpose (a restore from the Trash finds them again; a missing favorite simply matches
        nothing) and history is query text, so it is unaffected: nothing to do here."""
        return None

    # ------------------------------------------------------------------ backup
    def backup_if_due(self) -> bool:
        """Write ``<db>.bak`` (rotating the previous one to ``.bak.1``) once a day.
        Returns True if a backup was written."""
        if self.recovered == "memory":
            return False
        baks = self._backups()
        try:
            age = self.clock() - baks[0].stat().st_mtime
        except OSError:
            age = None
        if age is not None and age < self.BACKUP_EVERY:
            return False
        tmp = self.path.with_name(self.path.name + ".bak.tmp")
        try:
            with self._lock:
                dest = sqlite3.connect(str(tmp))
                try:
                    self._conn.backup(dest)
                finally:
                    dest.close()
            for i in range(len(baks) - 1, 0, -1):
                if baks[i - 1].exists():
                    os.replace(baks[i - 1], baks[i])
            os.replace(tmp, baks[0])
            return True
        except (sqlite3.Error, OSError) as exc:
            log.warning("user data backup failed: %s", exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
