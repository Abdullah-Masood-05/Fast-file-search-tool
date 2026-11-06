"""Durable index store: SQLite schema v5 (design A.3).

Plain ``dirs`` / ``entries`` tables with raw names; search keys are derived in memory by
``MemIndex``. The store is written by exactly one thread (the engine's writer) and is not
thread-safe. Every ``apply`` is one ``BEGIN IMMEDIATE ... COMMIT`` that bumps
``meta.generation``; the same op list is then applied to the in-memory index.

Names and paths that are not valid Unicode (POSIX surrogate-escaped bytes, unpaired NTFS
surrogates) are stored with ``encode_db_text`` so they round-trip exactly.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from .ops import DEL, DIR, DIRMTIME, DIRPATH, INS, REN, ROOTDEL, UPD

log = logging.getLogger(__name__)

SCHEMA_VERSION = 5
DB_NAME = "index-v5.db"
LEGACY_DB_NAME = "index.db"
LOAD_CHUNK = 50_000
_KEEP_CORRUPT = 2
_SQL_VARS = 500

# ---------------------------------------------------------------------------- db text

# File names are not always valid Unicode: POSIX names with non-UTF-8 bytes decode to lone
# surrogates (surrogateescape) and NTFS allows unpaired UTF-16 surrogates. SQLite needs valid
# UTF-8, so every surrogate code point is stored as NUL + 4 hex digits. NUL never occurs in a
# real path, so the escape is unambiguous and decode_db_text() restores the exact str.
_DB_ESCAPE = "\x00"
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_DB_ESCAPED_RE = re.compile("\x00([0-9a-f]{4})")


def encode_db_text(value: str) -> str:
    """Path text -> form storable in SQLite (identity for valid Unicode)."""
    if value.isascii() or not _SURROGATE_RE.search(value):
        return value
    return _SURROGATE_RE.sub(lambda m: f"{_DB_ESCAPE}{ord(m.group()):04x}", value)


def decode_db_text(value):
    """Inverse of :func:`encode_db_text`."""
    if isinstance(value, str) and _DB_ESCAPE in value:
        return _DB_ESCAPED_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
    return value


# ---------------------------------------------------------------------------- types

class StoreCorrupt(Exception):
    """The database file is unusable (corrupt, not a database, or unreadable)."""


class StoreTooNew(StoreCorrupt):
    """The database was written by a newer schema; it is set aside and rebuilt."""


class RootRow(NamedTuple):
    id: int
    path: str
    state: str
    entries: int
    folders: int
    bytes: int
    denied_dirs: int
    last_scan_end: Optional[int]
    last_scan_complete: int


class LoadChunk(NamedTuple):
    """One step of ``Store.iter_load``. The first chunk carries every root and dir; each
    chunk carries up to 50k entries ``(id, dir_id, name, size, mtime, ctime, flags)`` in id
    order. Dir rows are ``(id, parent_id, root_id, path, mtime, flags)``."""
    entries: list
    dirs: list
    roots: list


_ROOT_FIELDS = frozenset({
    "path", "state", "last_scan_start", "last_scan_end", "last_scan_complete", "entries", "folders",
    "bytes", "denied_dirs", "volume_id", "fsevents_last_id",
})

_SCHEMA = """
CREATE TABLE meta   (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE roots  (id INTEGER PRIMARY KEY,
                     path TEXT NOT NULL UNIQUE,
                     added_at INTEGER NOT NULL,
                     state TEXT NOT NULL DEFAULT 'new',
                     last_scan_start INTEGER, last_scan_end INTEGER,
                     last_scan_complete INTEGER NOT NULL DEFAULT 0,
                     entries INTEGER NOT NULL DEFAULT 0, folders INTEGER NOT NULL DEFAULT 0,
                     bytes INTEGER NOT NULL DEFAULT 0, denied_dirs INTEGER NOT NULL DEFAULT 0,
                     volume_id TEXT, fsevents_last_id INTEGER);
CREATE TABLE dirs   (id INTEGER PRIMARY KEY,
                     parent_id INTEGER NOT NULL,
                     root_id INTEGER NOT NULL,
                     path TEXT NOT NULL,
                     mtime INTEGER NOT NULL DEFAULT 0, flags INTEGER NOT NULL DEFAULT 0);
CREATE TABLE entries(id INTEGER PRIMARY KEY, dir_id INTEGER NOT NULL, name TEXT NOT NULL,
                     size INTEGER NOT NULL, mtime INTEGER NOT NULL, ctime INTEGER NOT NULL,
                     flags INTEGER NOT NULL);
CREATE UNIQUE INDEX dirs_path ON dirs(path);
CREATE INDEX dirs_parent ON dirs(parent_id);
CREATE UNIQUE INDEX entries_dir_name ON entries(dir_id, name);
"""

_SUBTREE_SQL = (
    "WITH RECURSIVE s(id) AS (SELECT ? UNION SELECT d.id FROM dirs d JOIN s ON d.parent_id = s.id) "
    "INSERT OR IGNORE INTO temp._sub(id) SELECT id FROM s"
)


def looks_corrupt(exc: BaseException) -> bool:
    """True for SQLite errors that mean the file itself is damaged."""
    if isinstance(exc, StoreCorrupt):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("malformed", "not a database", "corrupt", "disk image", "encrypted",
                                  "file is not a database"))


def _chunks(seq: Sequence, size: int = _SQL_VARS) -> Iterator[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------- store

class Store:
    """Single-writer SQLite store. NOT thread-safe; owned by the writer thread."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pending_roots: Dict[int, Dict[str, object]] = {}
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False,
                                         timeout=30)
            self._open()
        except sqlite3.DatabaseError as exc:
            self._close_quietly()
            raise StoreCorrupt(f"{self.path.name}: {exc}") from exc
        except StoreCorrupt:
            self._close_quietly()
            raise

    # ------------------------------------------------------------------ open / schema
    def _open(self) -> None:
        c = self._conn
        assert c is not None
        c.execute("PRAGMA busy_timeout=30000")
        fresh = c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        if fresh:
            c.execute("PRAGMA page_size=8192")       # must precede the first table
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA cache_size=-65536")
        c.execute("PRAGMA journal_size_limit=67108864")
        c.execute("PRAGMA temp_store=MEMORY")
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreTooNew(f"index schema v{version} is newer than supported v{SCHEMA_VERSION}")
        if fresh or version != SCHEMA_VERSION:
            # The index is derived data: an older or foreign layout is simply rebuilt.
            self._create(drop=not fresh)
        c.execute("CREATE TEMP TABLE IF NOT EXISTS _sub(id INTEGER PRIMARY KEY)")
        meta = dict(c.execute("SELECT key, value FROM meta"))
        self._generation = int(meta.get("generation", 0))
        hi = c.execute("SELECT max(m) FROM (SELECT max(id) AS m FROM entries UNION ALL "
                       "SELECT max(id) FROM dirs)").fetchone()[0] or 0
        self._next_id = max(int(meta.get("next_id", 1)), hi + 1)
        self._saved_next_id = int(meta.get("next_id", 1))

    def _create(self, drop: bool) -> None:
        c = self._conn
        assert c is not None
        c.execute("BEGIN IMMEDIATE")
        try:
            if drop:
                for (kind, name) in c.execute(
                        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index') "
                        "AND name NOT LIKE 'sqlite_%'").fetchall():
                    if kind == "table":
                        c.execute(f'DROP TABLE IF EXISTS "{name}"')
            for stmt in _SCHEMA.strip().split(";"):
                if stmt.strip():
                    c.execute(stmt)
            now = int(time.time())
            c.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [
                ("schema", str(SCHEMA_VERSION)), ("app_version", ""), ("created_at", str(now)),
                ("generation", "0"), ("next_id", "1"), ("clean_shutdown", "1"), ("last_vacuum", "0"),
            ])
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    @staticmethod
    def open_or_recover(path: Path) -> Tuple["Store", bool]:
        """Open the store; a corrupt, unreadable or too-new file (or one that fails
        ``quick_check`` after an unclean shutdown) is renamed to ``<name>.corrupt-<ts>``
        (newest 2 kept) and a fresh store is created. Returns ``(store, recovered)``."""
        path = Path(path)
        try:
            store = Store(path)
            if store.meta("clean_shutdown") == "0" and not store.quick_check():
                store.close(clean=False)
                raise StoreCorrupt("quick_check failed")
            return store, False
        except StoreCorrupt as exc:
            log.warning("index store unusable (%s); rebuilding", exc)
            quarantine(path)
            return Store(path), True

    def _close_quietly(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    # ------------------------------------------------------------------ properties
    @property
    def generation(self) -> int:
        return self._generation

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "store is closed"
        return self._conn

    def meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: object) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def quick_check(self) -> bool:
        try:
            row = self.conn.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.DatabaseError:
            return False
        return bool(row) and str(row[0]).lower() == "ok"

    def set_fast_writes(self, on: bool) -> None:
        """``synchronous=OFF`` for the first scan of an empty DB, ``NORMAL`` otherwise."""
        self.conn.execute(f"PRAGMA synchronous={'OFF' if on else 'NORMAL'}")

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        return total

    # ------------------------------------------------------------------ ids and roots
    def allocate_ids(self, n: int) -> range:
        """Fresh, monotonically increasing ids. Persisted with the next ``apply``; ids handed
        out but never committed are simply skipped after a crash."""
        start = self._next_id
        self._next_id += max(0, int(n))
        return range(start, self._next_id)

    def roots(self) -> List[RootRow]:
        rows = self.conn.execute(
            "SELECT id, path, state, entries, folders, bytes, denied_dirs, last_scan_end, "
            "last_scan_complete FROM roots ORDER BY id").fetchall()
        out = []
        for r in rows:
            row = RootRow(r[0], decode_db_text(r[1]), *r[2:])
            pend = self._pending_roots.get(row.id)
            if pend:
                row = row._replace(**{k: v for k, v in pend.items() if k in RootRow._fields})
            out.append(row)
        return out

    def add_root(self, path: str) -> int:
        """Insert a ``roots`` row and its ``DIR`` row (parent 0) in one transaction and return
        the root id (== the dir id). An already configured path returns its existing id.
        The caller mirrors it into the in-memory index with
        ``memindex.apply([(DIR, rid, 0, rid, path, 0)], store.generation)``."""
        enc = encode_db_text(path)
        row = self.conn.execute("SELECT id FROM roots WHERE path = ?", (enc,)).fetchone()
        if row:
            return int(row[0])
        rid = self.allocate_ids(1)[0]
        self._transaction([(DIR, rid, 0, rid, path, 0)], roots=[(rid, enc)])
        return rid

    def set_root_state(self, root_id: int, **fields) -> None:
        """Update ``roots`` columns. The change is written inside the next ``apply`` batch
        (or at ``close``); ``roots()`` reflects it immediately."""
        bad = set(fields) - _ROOT_FIELDS
        if bad:
            raise ValueError(f"unknown roots fields: {sorted(bad)}")
        self._pending_roots.setdefault(int(root_id), {}).update(fields)

    # ------------------------------------------------------------------ lookups
    def dir_lookup(self, path: str) -> Optional[Tuple[int, int]]:
        row = self.conn.execute("SELECT id, root_id FROM dirs WHERE path = ?",
                                (encode_db_text(path),)).fetchone()
        return (int(row[0]), int(row[1])) if row else None

    def children(self, dir_id: int) -> List[Tuple[int, str, int, int, int, int]]:
        rows = self.conn.execute("SELECT id, name, size, mtime, ctime, flags FROM entries WHERE dir_id = ?",
                                 (dir_id,)).fetchall()
        return [(r[0], decode_db_text(r[1]), r[2], r[3], r[4], r[5]) if _DB_ESCAPE in r[1] else r for r in rows]

    def entry_lookup(self, dir_id: int, name: str) -> Optional[Tuple[int, int, int, int, int]]:
        row = self.conn.execute("SELECT id, size, mtime, ctime, flags FROM entries WHERE dir_id = ? AND name = ?",
                                (dir_id, encode_db_text(name))).fetchone()
        return tuple(row) if row else None  # type: ignore[return-value]

    def dir_row(self, dir_id: int) -> Optional[Tuple[int, int, int, str, int, int]]:
        row = self.conn.execute("SELECT id, parent_id, root_id, path, mtime, flags FROM dirs WHERE id = ?",
                                (dir_id,)).fetchone()
        return (row[0], row[1], row[2], decode_db_text(row[3]), row[4], row[5]) if row else None

    def subdirs(self, dir_id: int) -> List[Tuple[int, str, int, int]]:
        """Direct child dirs: ``(id, path, mtime, flags)``."""
        rows = self.conn.execute("SELECT id, path, mtime, flags FROM dirs WHERE parent_id = ?", (dir_id,)).fetchall()
        return [(r[0], decode_db_text(r[1]), r[2], r[3]) for r in rows]

    def counts(self) -> Tuple[int, int]:
        """(entries, dirs) row counts: for tests and diagnostics only (full scans)."""
        c = self.conn
        return (c.execute("SELECT count(*) FROM entries").fetchone()[0],
                c.execute("SELECT count(*) FROM dirs").fetchone()[0])

    # ------------------------------------------------------------------ writes
    def apply(self, ops: Sequence[tuple]) -> int:
        """Apply ``ops`` in one transaction (pending root updates included); returns the new
        generation. On error the transaction is rolled back and nothing changes."""
        return self._transaction(ops)

    def _transaction(self, ops: Sequence[tuple], roots: Sequence[Tuple[int, str]] = ()) -> int:
        c = self.conn
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            raise StoreCorrupt(str(exc)) if looks_corrupt(exc) else exc
        try:
            now = int(time.time())
            for rid, enc in roots:
                c.execute("INSERT INTO roots (id, path, added_at) VALUES (?, ?, ?)", (rid, enc, now))
            for code, run in groupby(ops, key=itemgetter(0)):
                _HANDLERS[code](c, list(run))
            if self._pending_roots:
                for rid, fields in self._pending_roots.items():
                    cols = ", ".join(f"{k} = ?" for k in fields)
                    vals = [encode_db_text(v) if k == "path" and isinstance(v, str) else v
                            for k, v in fields.items()]
                    c.execute(f"UPDATE roots SET {cols} WHERE id = ?", (*vals, rid))
            gen = self._generation + 1
            c.execute("UPDATE meta SET value = ? WHERE key = 'generation'", (str(gen),))
            if self._next_id != self._saved_next_id:
                c.execute("UPDATE meta SET value = ? WHERE key = 'next_id'", (str(self._next_id),))
            c.execute("COMMIT")
        except BaseException as exc:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.DatabaseError) and looks_corrupt(exc):
                raise StoreCorrupt(str(exc)) from exc
            raise
        self._pending_roots.clear()
        self._saved_next_id = self._next_id
        self._generation = gen
        return gen

    # ------------------------------------------------------------------ load
    def iter_load(self) -> Iterator[LoadChunk]:
        """Everything needed to build the in-memory index (see ``LoadChunk``)."""
        c = self.conn
        roots = self.roots()
        dirs = [(r[0], r[1], r[2], decode_db_text(r[3]), r[4], r[5]) if _DB_ESCAPE in r[3] else r
                for r in c.execute("SELECT id, parent_id, root_id, path, mtime, flags FROM dirs ORDER BY id")]
        cur = c.execute("SELECT id, dir_id, name, size, mtime, ctime, flags FROM entries ORDER BY id")
        first = True
        while True:
            rows = cur.fetchmany(LOAD_CHUNK)
            if not rows and not first:
                break
            if rows and any(_DB_ESCAPE in r[2] for r in rows):
                rows = [(r[0], r[1], decode_db_text(r[2]), *r[3:]) for r in rows]
            yield LoadChunk(rows, dirs if first else [], roots if first else [])
            first = False
            if not rows:
                break

    # ------------------------------------------------------------------ lifecycle
    def mark_open(self) -> bool:
        """Set ``clean_shutdown=0``; return whether the previous shutdown was clean."""
        prev = self.meta("clean_shutdown") != "0"
        self.set_meta("clean_shutdown", 0)
        return prev

    def close(self, clean: bool = True) -> None:
        if self._conn is None:
            return
        try:
            if self._pending_roots:
                self._transaction(())
            if clean:
                try:
                    self._conn.execute("PRAGMA optimize")
                except sqlite3.Error:
                    pass
            self.set_meta("clean_shutdown", 1 if clean else 0)
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        except sqlite3.Error as exc:
            log.warning("closing the index store: %s", exc)
        finally:
            self._close_quietly()

    @staticmethod
    def remove_legacy(cache_dir: Path) -> List[str]:
        """Delete the v1 index (``index.db``, ``-wal``, ``-shm``, ``*.corrupt``; derived data,
        possibly GBs). Returns the removed paths. History and favorites live elsewhere."""
        cache_dir = Path(cache_dir)
        removed: List[str] = []
        try:
            names = os.listdir(cache_dir)
        except OSError:
            return removed
        for name in names:
            if name in (LEGACY_DB_NAME, LEGACY_DB_NAME + "-wal", LEGACY_DB_NAME + "-shm") or (
                    name.startswith(LEGACY_DB_NAME + ".") and name.endswith(".corrupt")):
                try:
                    os.remove(cache_dir / name)
                    removed.append(str(cache_dir / name))
                except OSError as exc:
                    log.warning("could not remove legacy index file %s: %s", name, exc)
        return removed


def quarantine(path: Path) -> Optional[Path]:
    """Rename a broken store to ``<name>.corrupt-<ts>`` (keeping the newest 2) and delete its
    ``-wal`` / ``-shm`` side files. Returns the new name (None if nothing was moved)."""
    path = Path(path)
    target: Optional[Path] = None
    if path.exists():
        target = path.with_name(f"{path.name}.corrupt-{int(time.time() * 1000)}")
        try:
            os.replace(path, target)
        except OSError:
            target = None
            try:
                os.remove(path)
            except OSError:
                pass
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(str(path) + suffix)
        except OSError:
            pass
    old = sorted(path.parent.glob(path.name + ".corrupt-*"), key=lambda p: p.name, reverse=True)
    for extra in old[_KEEP_CORRUPT:]:
        try:
            extra.unlink()
        except OSError:
            pass
    return target


# ---------------------------------------------------------------------------- op handlers

def _ins(c: sqlite3.Connection, run: list) -> None:
    c.executemany("INSERT INTO entries (id, dir_id, name, size, mtime, ctime, flags) VALUES (?,?,?,?,?,?,?)",
                  [(o[1], o[2], encode_db_text(o[3]), o[4], o[5], o[6], o[7]) for o in run])


def _upd(c: sqlite3.Connection, run: list) -> None:
    c.executemany("UPDATE entries SET size = ?, mtime = ?, ctime = ?, flags = ? WHERE id = ?",
                  [(o[2], o[3], o[4], o[5], o[1]) for o in run])


def _ren(c: sqlite3.Connection, run: list) -> None:
    c.executemany("UPDATE entries SET dir_id = ?, name = ? WHERE id = ?",
                  [(o[2], encode_db_text(o[3]), o[1]) for o in run])
    # A renamed/moved folder keeps its dir id; re-parent its dirs row (paths follow via DIRPATH).
    c.executemany("UPDATE dirs SET parent_id = ?, root_id = coalesce((SELECT root_id FROM dirs WHERE id = ?), "
                  "root_id) WHERE id = ?", [(o[2], o[2], o[1]) for o in run])


def _subtree(c: sqlite3.Connection, dir_id: int) -> None:
    c.execute("DELETE FROM temp._sub")
    c.execute(_SUBTREE_SQL, (dir_id,))


def _del(c: sqlite3.Connection, run: list) -> None:
    ids = [o[1] for o in run]
    dirs: set = set()
    for sub in _chunks(ids):
        dirs.update(r[0] for r in c.execute(
            f"SELECT id FROM dirs WHERE id IN ({','.join('?' * len(sub))})", sub))
    plain = [(i,) for i in ids if i not in dirs]
    if plain:
        c.executemany("DELETE FROM entries WHERE id = ?", plain)
    for d in dirs:
        _subtree(c, d)
        c.execute("DELETE FROM entries WHERE dir_id IN (SELECT id FROM temp._sub)")
        c.execute("DELETE FROM entries WHERE id IN (SELECT id FROM temp._sub)")
        c.execute("DELETE FROM dirs WHERE id IN (SELECT id FROM temp._sub)")


def _dir(c: sqlite3.Connection, run: list) -> None:
    c.executemany(
        "INSERT INTO dirs (id, parent_id, root_id, path, mtime) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET parent_id = excluded.parent_id, root_id = excluded.root_id, "
        "path = excluded.path, mtime = excluded.mtime",
        [(o[1], o[2], o[3], encode_db_text(o[4]), o[5]) for o in run])


def _dirpath(c: sqlite3.Connection, run: list) -> None:
    for o in run:
        row = c.execute("SELECT path, root_id FROM dirs WHERE id = ?", (o[1],)).fetchone()
        if row is None:
            continue
        old, root_id = row
        new = encode_db_text(o[2])
        _subtree(c, o[1])
        c.execute("UPDATE dirs SET path = ? || substr(path, ?), root_id = ? WHERE id IN (SELECT id FROM temp._sub)",
                  (new, len(old) + 1, root_id))


def _dirmtime(c: sqlite3.Connection, run: list) -> None:
    c.executemany("UPDATE dirs SET mtime = ?, flags = ? WHERE id = ?", [(o[2], o[3], o[1]) for o in run])


def _rootdel(c: sqlite3.Connection, run: list) -> None:
    for o in run:
        rid = o[1]
        c.execute("DELETE FROM entries WHERE dir_id IN (SELECT id FROM dirs WHERE root_id = ?)", (rid,))
        c.execute("DELETE FROM entries WHERE id = ?", (rid,))
        c.execute("DELETE FROM dirs WHERE root_id = ?", (rid,))
        c.execute("DELETE FROM roots WHERE id = ?", (rid,))


_HANDLERS = {INS: _ins, UPD: _upd, REN: _ren, DEL: _del, DIR: _dir, DIRPATH: _dirpath,
             DIRMTIME: _dirmtime, ROOTDEL: _rootdel}
