from __future__ import annotations

import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from .utils.file_utils import search_key
from .utils.platform_utils import get_cache_dir, paths_equal

SCHEMA_VERSION = 4

# Rows written per transaction by add_files_batch (readers see progress between
# chunks and the WAL stays small).
_WRITE_CHUNK = 2000
# Stay well below SQLITE_MAX_VARIABLE_NUMBER (999 on old builds).
_SQL_VAR_CHUNK = 500
# Never store more than this many characters of extracted content per file.
MAX_STORED_CONTENT_CHARS = 256 * 1024
# Longest query (in distinct trigrams) used for the index intersection; longer
# queries are still verified against the full substring so results stay exact.
_MAX_QUERY_TRIGRAMS = 32
# Only run PRAGMA quick_check on open for databases smaller than this.
_QUICK_CHECK_MAX_BYTES = 256 * 1024 * 1024

_FILE_COLUMNS = (
    "id, path, name, extension, size, modified, created, is_folder, "
    "parent_path, content_hash"
)

_CREATE_VERSION_SQL = """
CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _py_lower(value):
    return value.lower() if isinstance(value, str) else value


# File names are not always valid Unicode: POSIX names with non-UTF-8 bytes
# decode to lone surrogates (surrogateescape) and NTFS allows unpaired UTF-16
# surrogates. SQLite needs valid UTF-8, so path/name/parent_path store every
# surrogate code point as NUL + 4 hex digits. NUL can never occur in a real path,
# so the escape is unambiguous and decode_db_text() restores the exact str (the
# file stays openable). Search keys (name_lower, trigrams) use U+FFFD instead.
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


def _decode_row(row) -> dict:
    d = dict(row)
    for key in ("path", "name", "parent_path"):
        if key in d:
            d[key] = decode_db_text(d[key])
    return d


def _py_key_changed(name, name_lower):
    if not isinstance(name, str) or name.isascii():
        return 0
    return int(search_key(decode_db_text(name)) != name_lower)


def _migrate_4(conn: sqlite3.Connection) -> None:
    """v4: name search keys are NFC-normalized (macOS returns decomposed names),
    so recompute name_lower and trigrams for names whose key changed."""
    rows = conn.execute(
        "SELECT id, name FROM files WHERE py_key_changed(name, name_lower)"
    ).fetchall()
    for fid, name in rows:
        key = search_key(decode_db_text(name))
        conn.execute("UPDATE files SET name_lower = ? WHERE id = ?", (key, fid))
        conn.execute("DELETE FROM file_trigrams WHERE file_id = ?", (fid,))
        grams = list({key[i : i + 3] for i in range(len(key) - 2)})
        conn.executemany(
            "INSERT OR IGNORE INTO trigrams (trigram) VALUES (?)", ((g,) for g in grams)
        )
        conn.executemany(
            "INSERT OR IGNORE INTO file_trigrams (file_id, trigram_id) "
            "SELECT ?, id FROM trigrams WHERE trigram = ?",
            ((fid, g) for g in grams),
        )
    conn.execute("UPDATE metadata SET value = '4' WHERE key = 'schema_version'")


def _migrate_3(conn: sqlite3.Connection) -> None:
    """v3: unicode-safe lowercase name column, leaner trigram indexes.

    * ``name_lower`` (Python ``str.lower``; SQLite's LOWER is ASCII-only).
    * drop short (<3 chars) pseudo trigrams written by older versions.
    * drop indexes duplicating UNIQUE constraints, add a covering index for
      trigram -> file_id intersection.
    * content_text was never read by anything and bloated the DB: clear it.
    """
    conn.execute("ALTER TABLE files ADD COLUMN name_lower TEXT")
    conn.execute("UPDATE files SET name_lower = py_lower(name)")
    conn.execute(
        "DELETE FROM file_trigrams WHERE trigram_id IN "
        "(SELECT id FROM trigrams WHERE length(trigram) < 3)"
    )
    conn.execute("DELETE FROM trigrams WHERE length(trigram) < 3")
    conn.execute("DROP INDEX IF EXISTS idx_files_path")
    conn.execute("DROP INDEX IF EXISTS idx_trigrams_trigram")
    conn.execute("DROP INDEX IF EXISTS idx_ft_trigram_id")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ft_trigram_file "
        "ON file_trigrams(trigram_id, file_id)"
    )
    conn.execute(
        "UPDATE files SET content_text = NULL WHERE content_text IS NOT NULL"
    )
    conn.execute("UPDATE metadata SET value = '3' WHERE key = 'schema_version'")


_Migration = Union[List[str], Callable[[sqlite3.Connection], None]]

_MIGRATIONS: Dict[int, _Migration] = {
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
    3: _migrate_3,
    4: _migrate_4,
}

_UPSERT_SQL = """
INSERT INTO files (path, name, name_lower, extension, size, modified, created,
                   is_folder, parent_path, content_hash, content_text)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    name = excluded.name,
    name_lower = excluded.name_lower,
    extension = excluded.extension,
    size = excluded.size,
    modified = excluded.modified,
    created = excluded.created,
    is_folder = excluded.is_folder,
    parent_path = excluded.parent_path,
    content_hash = excluded.content_hash,
    content_text = excluded.content_text
"""


class _IncompatibleSchema(sqlite3.DatabaseError):
    """The on-disk database was written by a newer schema version."""


def _looks_corrupt(exc: sqlite3.Error) -> bool:
    if isinstance(exc, _IncompatibleSchema):
        return True
    msg = str(exc).lower()
    return any(
        s in msg
        for s in ("malformed", "not a database", "corrupt", "disk image", "encrypted")
    )


def _chunks(seq: Sequence, size: int) -> Iterator[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _descendant_clause(prefix: str, column: str = "path") -> Tuple[str, tuple]:
    """SQL (index-friendly range scan) matching ``prefix`` itself and everything below it.

    On Windows both ``/`` and ``\\`` separated stored paths match; on POSIX
    ``\\`` is an ordinary file-name character, so only ``/`` separates.
    Case-sensitive: stored paths are spelled exactly as the walk produced them.
    """
    prefix = encode_db_text(prefix)
    if sys.platform != "win32":
        base = prefix.rstrip("/")
        if not base:  # the filesystem root: everything below "/"
            return f"({column} >= ? AND {column} < ?)", ("/", "0")
        sql = f"({column} = ? OR {column} = ? OR ({column} >= ? AND {column} < ?))"
        return sql, (prefix, base, base + "/", base + "0")
    base = prefix.rstrip("/\\")
    sql = (
        f"({column} = ? OR {column} = ? "
        f"OR ({column} >= ? AND {column} < ?) "
        f"OR ({column} >= ? AND {column} < ?))"
    )
    # '/' + 1 == '0', '\\' + 1 == ']'
    params = (prefix, base, base + "/", base + "0", base + "\\", base + "]")
    return sql, params


class PersistentIndex:
    """SQLite-backed file index that survives restarts.

    Threading model: all writes go through ONE shared writer connection that is
    only used while holding ``self._lock`` (so worker threads never create
    write connections). Reads use a per-thread connection obtained via
    ``_get_conn``; connections of threads that have ended are closed lazily and
    ``close()`` closes every connection from every thread.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_cache_dir("Fast File Search Pro") / "index.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()  # the single writer lock
        self._conn_lock = threading.Lock()  # guards connection bookkeeping
        self._local = threading.local()
        self._readers: List[Tuple[threading.Thread, sqlite3.Connection]] = []
        self._writer: Optional[sqlite3.Connection] = None
        self._generation = 0
        self._tx_depth = 0
        self.recovered_from_corruption = False
        self._open_database()

    # ------------------------------------------------------------------ connections

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            isolation_level=None,  # explicit BEGIN/COMMIT, see _write()
            check_same_thread=False,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.create_function("py_lower", 1, _py_lower, deterministic=True)
            conn.create_function("py_key_changed", 2, _py_key_changed, deterministic=True)
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA cache_size=-64000;")
            conn.execute("PRAGMA temp_store=MEMORY;")
        except BaseException:
            conn.close()
            raise
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        """Per-thread connection for reads (do not write through it)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "gen", -1) == self._generation:
            return conn
        conn = self._connect()
        with self._conn_lock:
            self._prune_readers_locked()
            self._readers.append((threading.current_thread(), conn))
            self._local.gen = self._generation
        self._local.conn = conn
        return conn

    def _prune_readers_locked(self) -> None:
        alive = []
        for thread, conn in self._readers:
            if thread.is_alive():
                alive.append((thread, conn))
            else:
                try:
                    conn.close()
                except Exception:
                    pass
        self._readers = alive

    def release_thread_connection(self) -> None:
        """Close the calling thread's read connection (for worker threads)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._conn_lock:
            self._readers = [(t, c) for (t, c) in self._readers if c is not conn]
        try:
            conn.close()
        except Exception:
            pass

    def _get_writer(self) -> sqlite3.Connection:
        with self._conn_lock:
            if self._writer is None:
                self._writer = self._connect()
            return self._writer

    @contextmanager
    def _write(self):
        """Serialised write transaction (re-entrant)."""
        with self._lock:
            conn = self._get_writer()
            if self._tx_depth > 0:
                self._tx_depth += 1
                try:
                    yield conn
                finally:
                    self._tx_depth -= 1
                return
            conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                conn.execute("COMMIT")
            finally:
                self._tx_depth = 0

    def close(self) -> None:
        """Close every connection (all threads). The index can be reused afterwards;
        connections are reopened lazily."""
        with self._lock:
            with self._conn_lock:
                self._generation += 1
                writer, self._writer = self._writer, None
                readers, self._readers = self._readers, []
            if writer is not None:
                try:
                    writer.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except sqlite3.Error:
                    pass
                try:
                    writer.close()
                except Exception:
                    pass
            for _thread, conn in readers:
                try:
                    conn.close()
                except Exception:
                    pass
            self._local.conn = None

    # ------------------------------------------------------------------ open / recovery

    def _open_database(self) -> None:
        for attempt in (1, 2):
            try:
                self._init_schema()
                return
            except sqlite3.DatabaseError as exc:
                if not _looks_corrupt(exc) or attempt == 2:
                    raise
                self.close()
                self._quarantine_database()
                self.recovered_from_corruption = True

    def _quarantine_database(self) -> None:
        """Move a corrupt/incompatible database aside as ``<name>.corrupt``."""
        target = self.db_path.with_name(self.db_path.name + ".corrupt")
        if target.exists():
            target = self.db_path.with_name(
                f"{self.db_path.name}.{int(time.time())}.corrupt"
            )
        if self.db_path.exists():
            os.replace(self.db_path, target)
        for suffix in ("-wal", "-shm"):
            side = self.db_path.with_name(self.db_path.name + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError:
                pass

    def is_own_file(self, path: Union[str, Path]) -> bool:
        """True for this index's own files (db, -wal, -shm, .corrupt) so scanners
        and watchers never index (and re-trigger on) their own writes."""
        p = Path(path)
        return (
            p.name.startswith(self.db_path.name)
            and paths_equal(p.parent, self.db_path.parent)
        )

    def _init_schema(self) -> None:
        if (
            self.db_path.exists()
            and self.db_path.stat().st_size < _QUICK_CHECK_MAX_BYTES
        ):
            conn = self._get_writer()
            result = conn.execute("PRAGMA quick_check(1);").fetchone()
            if result is None or str(result[0]).lower() != "ok":
                raise sqlite3.DatabaseError(
                    f"database disk image is malformed: {result[0] if result else '?'}"
                )
        self._migrate()

    def _migrate(self) -> None:
        conn = self._get_writer()
        conn.execute(_CREATE_VERSION_SQL)
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        current = row[0] if row and row[0] else 0
        if current > SCHEMA_VERSION:
            raise _IncompatibleSchema(
                f"index schema v{current} is newer than supported v{SCHEMA_VERSION}"
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            migration = _MIGRATIONS.get(version)
            if migration is None:
                continue
            with self._write() as wconn:
                if callable(migration):
                    migration(wconn)
                else:
                    for stmt in migration:
                        wconn.execute(stmt)
                wconn.execute(
                    "INSERT INTO _schema_version (version) VALUES (?)", (version,)
                )

    # backwards-compatible name
    _create_tables = _migrate

    # ------------------------------------------------------------------ writes

    def _generate_trigrams(self, text: str) -> List[str]:
        """Distinct lowercase NFC 3-grams of ``text`` (empty for text shorter than 3)."""
        text = search_key(text)
        if len(text) < 3:
            return []
        return list({text[i : i + 3] for i in range(len(text) - 2)})

    @staticmethod
    def _prepare_row(info: dict) -> tuple:
        path = str(info["path"])
        name = info.get("name") or Path(path).name or path
        extension = info.get("extension")
        if extension is None:
            extension = Path(path).suffix.lower()
        parent_path = info.get("parent_path")
        if parent_path is None:
            parent_path = str(Path(path).parent)
        content = info.get("content_text")
        if content is not None and len(content) > MAX_STORED_CONTENT_CHARS:
            content = content[:MAX_STORED_CONTENT_CHARS]
        if content is not None and not content.isascii():
            content = content.encode("utf-8", "replace").decode("utf-8")
        return (
            encode_db_text(path),
            encode_db_text(name),
            search_key(name),
            extension if extension.isascii() else extension.encode("utf-8", "replace").decode("utf-8"),
            int(info.get("size") or 0),
            int(info.get("modified") or 0),
            int(info.get("created") or 0),
            1 if info.get("is_folder") else 0,
            encode_db_text(str(parent_path)),
            info.get("content_hash"),
            content,
        )

    def add_file(self, file_info: dict) -> None:
        self.add_files_batch([file_info])

    def add_files_batch(self, file_infos: Iterable[dict]) -> None:
        """Insert/update many files; one transaction (and one lock hold) per chunk."""
        infos = file_infos if isinstance(file_infos, list) else list(file_infos)
        for chunk in _chunks(infos, _WRITE_CHUNK):
            self._upsert_chunk(chunk)

    def _upsert_chunk(self, chunk: Sequence[dict]) -> None:
        rows: Dict[str, tuple] = {}
        for info in chunk:
            row = self._prepare_row(info)
            rows[row[0]] = row  # last write for a path wins
        if not rows:
            return
        paths = list(rows)

        with self._write() as conn:
            existing: Dict[str, Tuple[int, str]] = {}
            for sub in _chunks(paths, _SQL_VAR_CHUNK):
                ph = ",".join("?" * len(sub))
                for r in conn.execute(
                    f"SELECT id, path, name FROM files WHERE path IN ({ph})", sub
                ):
                    existing[r["path"]] = (r["id"], r["name"])

            conn.executemany(_UPSERT_SQL, rows.values())

            new_paths = [p for p in paths if p not in existing]
            ids: Dict[str, int] = {p: v[0] for p, v in existing.items()}
            for sub in _chunks(new_paths, _SQL_VAR_CHUNK):
                ph = ",".join("?" * len(sub))
                for r in conn.execute(
                    f"SELECT id, path FROM files WHERE path IN ({ph})", sub
                ):
                    ids[r["path"]] = r["id"]

            stale_ids: List[int] = []
            grams: set = set()
            pairs: List[Tuple[int, str]] = []
            for path, row in rows.items():
                fid = ids.get(path)
                if fid is None:
                    continue
                name = row[1]
                old = existing.get(path)
                if old is not None:
                    if old[1] == name:
                        continue  # trigrams depend only on the name
                    stale_ids.append(fid)
                file_grams = self._generate_trigrams(decode_db_text(name))
                grams.update(file_grams)
                pairs.extend((fid, g) for g in file_grams)

            for sub in _chunks(stale_ids, _SQL_VAR_CHUNK):
                ph = ",".join("?" * len(sub))
                conn.execute(f"DELETE FROM file_trigrams WHERE file_id IN ({ph})", sub)
            if grams:
                conn.executemany(
                    "INSERT OR IGNORE INTO trigrams (trigram) VALUES (?)",
                    ((g,) for g in grams),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO file_trigrams (file_id, trigram_id) "
                    "SELECT ?, id FROM trigrams WHERE trigram = ?",
                    pairs,
                )

    def remove_file(self, path: str) -> None:
        """Remove ``path``; if it is an indexed folder, everything below it too."""
        self.remove_files_batch([path])

    def remove_files_batch(
        self, paths: Iterable[str], include_descendants: bool = True
    ) -> int:
        """Remove paths (chunked to respect SQLite's variable limit).

        Folders also remove their indexed descendants unless
        ``include_descendants`` is False. Returns the number of rows deleted.
        """
        paths = [encode_db_text(str(p)) for p in paths]
        if not paths:
            return 0
        removed = 0
        with self._write() as conn:
            if include_descendants:
                folders: List[str] = []
                for sub in _chunks(paths, _SQL_VAR_CHUNK):
                    ph = ",".join("?" * len(sub))
                    folders.extend(
                        r[0]
                        for r in conn.execute(
                            f"SELECT path FROM files WHERE is_folder = 1 AND path IN ({ph})",
                            sub,
                        )
                    )
                for folder in folders:
                    clause, params = _descendant_clause(decode_db_text(folder))
                    conn.execute(
                        f"DELETE FROM file_trigrams WHERE file_id IN "
                        f"(SELECT id FROM files WHERE {clause})",
                        params,
                    )
                    removed += conn.execute(
                        f"DELETE FROM files WHERE {clause}", params
                    ).rowcount
            for sub in _chunks(paths, _SQL_VAR_CHUNK):
                ph = ",".join("?" * len(sub))
                conn.execute(
                    f"DELETE FROM file_trigrams WHERE file_id IN "
                    f"(SELECT id FROM files WHERE path IN ({ph}))",
                    sub,
                )
                removed += conn.execute(
                    f"DELETE FROM files WHERE path IN ({ph})", sub
                ).rowcount
        if len(paths) >= _SQL_VAR_CHUNK:
            self.cleanup_orphan_trigrams()
        return removed

    def cleanup_orphan_trigrams(self) -> int:
        """Delete trigram rows no file references any more. Returns rows deleted."""
        with self._write() as conn:
            return conn.execute(
                "DELETE FROM trigrams WHERE NOT EXISTS "
                "(SELECT 1 FROM file_trigrams ft WHERE ft.trigram_id = trigrams.id)"
            ).rowcount

    def rebuild_index(self) -> None:
        """Wipe the whole index (all roots). Only for an explicit user rebuild."""
        with self._write() as conn:
            conn.execute("DELETE FROM file_trigrams")
            conn.execute("DELETE FROM trigrams")
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM metadata WHERE key != 'schema_version'")

    def update_metadata(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (key, str(value)),
            )

    # ------------------------------------------------------------------ reads

    def get_metadata(self, key: str) -> Optional[str]:
        row = self._get_conn().execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def is_empty(self) -> bool:
        return self._get_conn().execute("SELECT 1 FROM files LIMIT 1").fetchone() is None

    def get_index_stats(self) -> Dict[str, object]:
        """``file_count`` counts every indexed entry (folders included)."""
        conn = self._get_conn()
        file_count, folder_count, total_size = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_folder), 0), "
            "COALESCE(SUM(CASE WHEN is_folder = 0 THEN size ELSE 0 END), 0) FROM files"
        ).fetchone()
        trigram_count = conn.execute("SELECT COUNT(*) FROM trigrams").fetchone()[0]
        last_updated = self.get_metadata("last_updated")
        return {
            "file_count": file_count,
            "folder_count": folder_count,
            "total_size": total_size,
            "trigram_count": trigram_count,
            "last_updated": last_updated,
            "extension_distribution": self.get_top_extensions(20, with_counts=True),
            "avg_file_size": total_size // max(1, file_count - folder_count),
        }

    def get_top_extensions(self, limit: int = 15, with_counts: bool = False):
        rows = self._get_conn().execute(
            "SELECT extension, COUNT(*) AS cnt FROM files WHERE is_folder = 0 "
            "GROUP BY extension ORDER BY cnt DESC LIMIT ?",
            (limit,),
        ).fetchall()
        if with_counts:
            return {r["extension"]: r["cnt"] for r in rows}
        return [r["extension"] for r in rows if r["extension"]]

    def file_exists(self, path: str) -> bool:
        row = self._get_conn().execute(
            "SELECT 1 FROM files WHERE path = ?", (encode_db_text(path),)
        ).fetchone()
        return row is not None

    def get_file_by_path(self, path: str) -> Optional[dict]:
        row = self._get_conn().execute(
            f"SELECT {_FILE_COLUMNS}, content_text FROM files WHERE path = ?",
            (encode_db_text(path),),
        ).fetchone()
        return _decode_row(row) if row is not None else None

    def iter_all_files(self) -> Iterator[dict]:
        cursor = self._get_conn().execute(f"SELECT {_FILE_COLUMNS} FROM files")
        for row in cursor:
            yield _decode_row(row)

    def iter_file_states(
        self, root: Optional[str] = None
    ) -> Iterator[Tuple[str, int, int, bool]]:
        """Yield ``(path, size, modified, is_folder)`` for change detection,
        optionally restricted to entries at/below ``root``."""
        conn = self._get_conn()
        if root:
            clause, params = _descendant_clause(str(root))
            cursor = conn.execute(
                f"SELECT path, size, modified, is_folder FROM files WHERE {clause}",
                params,
            )
        else:
            cursor = conn.execute("SELECT path, size, modified, is_folder FROM files")
        for r in cursor:
            yield decode_db_text(r[0]), r[1], r[2], bool(r[3])

    def search_trigram(self, query: str, limit: int = 1000) -> List[dict]:
        """Case-insensitive substring search on file names.

        Queries of 3+ characters intersect the trigram postings in SQL and are
        verified against the real substring; shorter queries use a substring scan.
        """
        query_lower = search_key(query.strip())
        if not query_lower:
            return []
        grams = self._generate_trigrams(query_lower)
        if not grams:
            return self._search_like(query_lower, limit)

        conn = self._get_conn()
        grams = grams[:_MAX_QUERY_TRIGRAMS]
        ph = ",".join("?" * len(grams))
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM trigrams WHERE trigram IN ({ph})", grams
            )
        ]
        if len(ids) < len(grams):
            return []  # some trigram is in no file name at all
        ids_ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT {_FILE_COLUMNS} FROM files
                WHERE id IN (
                    SELECT file_id FROM file_trigrams
                    WHERE trigram_id IN ({ids_ph})
                    GROUP BY file_id HAVING COUNT(*) = ?)
                AND instr(COALESCE(name_lower, lower(name)), ?) > 0
                LIMIT ?""",
            (*ids, len(ids), query_lower, limit),
        ).fetchall()
        return [_decode_row(r) for r in rows]

    def _search_like(self, query_lower: str, limit: int = 1000) -> List[dict]:
        """Substring scan on the name column. Uses ``instr`` rather than LIKE so
        ``%`` and ``_`` in the query are matched literally."""
        rows = self._get_conn().execute(
            f"SELECT {_FILE_COLUMNS} FROM files "
            "WHERE instr(COALESCE(name_lower, lower(name)), ?) > 0 LIMIT ?",
            (query_lower, limit),
        ).fetchall()
        return [_decode_row(r) for r in rows]
