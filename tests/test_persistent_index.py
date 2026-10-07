import os
import sqlite3
import threading
import time

import pytest

from fast_file_search.config import AppConfig
from fast_file_search.indexer import Indexer
from fast_file_search.persistent_index import PersistentIndex


def _info(path, name=None, size=1, modified=1, is_folder=False):
    name = name or os.path.basename(path)
    return {
        "path": path,
        "name": name,
        "extension": os.path.splitext(name)[1].lower(),
        "size": size,
        "modified": modified,
        "created": modified,
        "is_folder": is_folder,
        "parent_path": os.path.dirname(path),
    }


def _make_indexer(db_path, exclude=None):
    cfg = AppConfig()
    cfg.exclude_patterns = list(exclude if exclude is not None else [".git"])
    return Indexer(config=cfg, persistent_index=PersistentIndex(db_path=db_path))


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "db" / "index.db"


# --------------------------------------------------------------------------- persistence


def test_index_survives_reopen(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_files_batch([_info("/a/report_2024.pdf", size=10), _info("/a/notes.txt")])
    idx.update_metadata("k", "v")
    idx.close()

    again = PersistentIndex(db_path=db_path)
    assert again.file_exists("/a/report_2024.pdf")
    assert again.get_file_by_path("/a/report_2024.pdf")["size"] == 10
    assert again.get_metadata("k") == "v"
    assert [r["name"] for r in again.search_trigram("report")] == ["report_2024.pdf"]
    assert not again.is_empty()
    again.close()


def test_wal_mode_and_no_data_loss_without_close(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_file(_info("/x/file_one.txt"))
    # never closed: a second handle on the same file must still see committed data
    other = PersistentIndex(db_path=db_path)
    assert other.file_exists("/x/file_one.txt")
    assert other._get_conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    other.close()
    idx.close()


def test_incremental_update_after_restart_touches_only_changes(tmp_path, db_path):
    root = tmp_path / "root"
    for i in range(20):
        _write(root / "sub" / f"file_{i:02}.txt", "hello")
    _write(root / "gone.txt")
    _write(root / "modified.txt", "one")

    first = _make_indexer(db_path)
    s1 = first.incremental_update(str(root))
    assert s1.added == 23  # 22 files + the "sub" folder (the root itself is not stored)
    first.persistent_index.close()

    # ---- "restart": brand new objects on the same db file
    (root / "gone.txt").unlink()
    _write(root / "new_file.txt")
    _write(root / "modified.txt", "a much longer body than before")

    second = _make_indexer(db_path)
    written = []
    real_add = second.persistent_index.add_files_batch
    second.persistent_index.add_files_batch = lambda infos: (
        written.extend(i["path"] for i in infos),
        real_add(infos),
    )[1]
    s2 = second.incremental_update(str(root))

    assert s2.added == 1
    assert s2.updated == 1
    assert s2.removed == 1
    assert s2.unchanged == s1.added - 2  # all but the deleted and the modified file
    assert sorted(os.path.basename(p) for p in written) == ["modified.txt", "new_file.txt"]
    pi = second.persistent_index
    assert not pi.file_exists(str(root / "gone.txt"))
    assert pi.file_exists(str(root / "new_file.txt"))
    assert pi.get_file_by_path(str(root / "modified.txt"))["size"] == len("a much longer body than before")

    # a third run with no changes writes nothing
    written.clear()
    s3 = second.incremental_update(str(root))
    assert (s3.added, s3.updated, s3.removed) == (0, 0, 0)
    assert written == []
    pi.close()


def test_rescan_does_not_wipe_other_roots(tmp_path, db_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / "alpha_file.txt")
    _write(b / "beta_file.txt")
    ix = _make_indexer(db_path)
    ix.incremental_update(str(a))
    ix.incremental_update(str(b))
    ix.index_path(str(a))  # forced full re-read of one root
    ix.incremental_update(str(b))
    assert ix.persistent_index.file_exists(str(a / "alpha_file.txt"))
    assert ix.persistent_index.file_exists(str(b / "beta_file.txt"))
    ix.persistent_index.close()


def test_index_path_removes_stale_entries_only_under_root(tmp_path, db_path):
    a = tmp_path / "a"
    _write(a / "keep.txt")
    _write(a / "drop.txt")
    ix = _make_indexer(db_path)
    ix.index_path(str(a))
    ix.persistent_index.add_file(_info("/elsewhere/unrelated.txt"))
    (a / "drop.txt").unlink()
    ix.index_path(str(a))
    pi = ix.persistent_index
    assert pi.file_exists(str(a / "keep.txt"))
    assert not pi.file_exists(str(a / "drop.txt"))
    assert pi.file_exists("/elsewhere/unrelated.txt")
    pi.close()


def test_folders_are_indexed_as_folders(tmp_path, db_path):
    root = tmp_path / "r"
    _write(root / "inner" / "f.txt")
    ix = _make_indexer(db_path)
    stats = ix.incremental_update(str(root))
    assert stats.total_folders == 1 and stats.total_files == 1
    row = ix.persistent_index.get_file_by_path(str(root / "inner"))
    assert row["is_folder"] == 1
    ix.persistent_index.close()


def test_metadata_index_stats_is_json(tmp_path, db_path):
    import json

    root = tmp_path / "r"
    _write(root / "f.txt")
    ix = _make_indexer(db_path)
    ix.incremental_update(str(root))
    data = json.loads(ix.persistent_index.get_metadata("index_stats"))
    assert data["total_files"] == 1
    assert ix.persistent_index.get_metadata("last_updated")
    ix.persistent_index.close()


# --------------------------------------------------------------------------- exclusion


def test_exclude_matches_path_components_not_substrings(tmp_path, db_path):
    root = tmp_path / "tmp" / "proj"  # an ancestor named like an exclude pattern
    _write(root / "tmp" / "excluded.txt")
    _write(root / "tmpl" / "template.txt")
    _write(root / "var" / "data.txt")
    _write(root / ".git" / "config")
    _write(root / ".github" / "workflow.yml")
    _write(root / "sys" / "mine.txt")
    ix = _make_indexer(db_path, exclude=["tmp", ".git"])
    ix.incremental_update(str(root))
    pi = ix.persistent_index
    assert not pi.file_exists(str(root / "tmp" / "excluded.txt"))
    assert not pi.file_exists(str(root / ".git" / "config"))
    assert pi.file_exists(str(root / "tmpl" / "template.txt"))
    assert pi.file_exists(str(root / ".github" / "workflow.yml"))
    # 'var' and 'sys' are legitimate user folder names (esp. on Windows)
    assert pi.file_exists(str(root / "var" / "data.txt"))
    assert pi.file_exists(str(root / "sys" / "mine.txt"))
    pi.close()


def test_own_database_files_are_never_indexed(tmp_path):
    db = tmp_path / "index.db"
    _write(tmp_path / "doc.txt")
    ix = _make_indexer(db)
    ix.incremental_update(str(tmp_path))
    names = {r["name"] for r in ix.persistent_index.iter_all_files()}
    assert "doc.txt" in names
    assert not any(n.startswith("index.db") for n in names)
    ix.persistent_index.close()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlink_loops_are_not_followed(tmp_path, db_path):
    root = tmp_path / "r"
    _write(root / "real" / "f.txt")
    try:
        os.symlink(root, root / "real" / "loop", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    ix = _make_indexer(db_path)
    ix.incremental_update(str(root))  # must terminate
    paths = [r["path"] for r in ix.persistent_index.iter_all_files()]
    assert not any("loop" + os.sep + "real" in p for p in paths)
    ix.persistent_index.close()


def test_stop_cancels_scan_and_keeps_db_usable(tmp_path, db_path):
    root = tmp_path / "r"
    for i in range(50):
        _write(root / f"f{i}.txt")
    ix = _make_indexer(db_path)
    ix.stop()
    stats = ix.incremental_update(str(root))
    assert stats.cancelled
    assert ix.persistent_index.is_empty()
    ix.reset()
    stats = ix.incremental_update(str(root))
    assert not stats.cancelled and stats.added == 50
    ix.persistent_index.close()


# --------------------------------------------------------------------------- removal


def test_remove_file_and_orphan_trigram_cleanup(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_file(_info("/a/zebra_unique.txt"))
    idx.add_file(_info("/a/other.txt"))
    assert idx.search_trigram("zebra")
    idx.remove_file("/a/zebra_unique.txt")
    assert not idx.file_exists("/a/zebra_unique.txt")
    assert idx.search_trigram("zebra") == []
    idx.cleanup_orphan_trigrams()
    conn = idx._get_conn()
    assert conn.execute("SELECT COUNT(*) FROM trigrams WHERE trigram='zeb'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM file_trigrams WHERE file_id NOT IN (SELECT id FROM files)"
    ).fetchone()[0] == 0
    idx.close()


def test_remove_files_batch_beyond_sqlite_variable_limit(db_path):
    idx = PersistentIndex(db_path=db_path)
    infos = [_info(f"/bulk/file_{i}.dat") for i in range(3000)]
    idx.add_files_batch(infos)
    idx.add_file(_info("/bulk/survivor.dat"))
    removed = idx.remove_files_batch([i["path"] for i in infos])
    assert removed == 3000
    assert [r["name"] for r in idx.iter_all_files()] == ["survivor.dat"]
    # trigram rows of removed files were cleaned up (large batch -> auto cleanup)
    assert idx._get_conn().execute("SELECT COUNT(*) FROM file_trigrams").fetchone()[0] == len(
        set("survivor.dat"[i : i + 3] for i in range(10))
    )
    idx.close()


def test_removing_folder_removes_its_subtree(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_files_batch(
        [
            _info("/proj", is_folder=True),
            _info("/proj/src", is_folder=True),
            _info("/proj/src/main.py"),
            _info("/proj2/main.py"),
        ]
    )
    idx.remove_file("/proj")
    left = {r["path"] for r in idx.iter_all_files()}
    assert left == {"/proj2/main.py"}  # '/proj2' is not under '/proj'
    idx.close()


# --------------------------------------------------------------------------- search


def test_search_correctness(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_files_batch(
        [
            _info("/d/Annual_Report_2024.PDF"),
            _info("/d/report.txt"),
            _info("/d/repo_rt.txt"),
            _info("/d/100%_done.txt"),
            _info("/d/50_off.txt"),
            _info("/d/ab.txt"),
            _info("/d/Übersicht.md"),
        ]
    )
    names = lambda q: sorted(r["name"] for r in idx.search_trigram(q))  # noqa: E731
    assert names("REPORT") == ["Annual_Report_2024.PDF", "report.txt"]
    assert names("port_20") == ["Annual_Report_2024.PDF"]  # '_' matches literally
    assert names("0%_d") == ["100%_done.txt"]  # '%' matches literally
    assert names("%") == ["100%_done.txt"]
    assert names("_") == ["100%_done.txt", "50_off.txt", "Annual_Report_2024.PDF", "repo_rt.txt"]
    assert names("ab") == ["ab.txt"]
    assert names("übers") == ["Übersicht.md"]  # unicode case folding
    assert names("nomatchhere") == []
    assert names("   ") == []
    assert len(idx.search_trigram("t", limit=2)) == 2
    idx.close()


def test_update_changes_trigrams_when_name_changes(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_file(_info("/d/f", name="oldname.txt"))
    idx.add_file(_info("/d/f", name="newtitle.txt"))
    assert idx.search_trigram("oldname") == []
    assert [r["path"] for r in idx.search_trigram("newtitle")] == ["/d/f"]
    assert idx._get_conn().execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    idx.close()


def test_search_result_shape_is_backward_compatible(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_file(_info("/d/shape.txt", size=7, modified=5))
    row = idx.search_trigram("shape")[0]
    for key in ("path", "name", "extension", "size", "modified", "created", "is_folder", "parent_path"):
        assert key in row
    assert "content_text" not in row  # big blobs are not shipped with search hits
    assert idx.get_index_stats()["file_count"] == 1
    idx.close()


def test_add_files_batch_is_fast_and_single_transaction(db_path):
    idx = PersistentIndex(db_path=db_path)
    infos = [_info(f"/big/dir{i % 50}/some_file_name_{i}.txt") for i in range(20000)]
    t0 = time.time()
    idx.add_files_batch(infos)
    elapsed = time.time() - t0
    assert elapsed < 30  # was minutes with per-row commits + per-trigram round trips
    assert idx.get_index_stats()["file_count"] == 20000
    assert len(idx.search_trigram("file_name_19999")) == 1
    idx.close()


# --------------------------------------------------------------------------- robustness


def test_corrupt_database_is_quarantined_and_recreated(db_path):
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"this is definitely not an sqlite database" * 100)
    idx = PersistentIndex(db_path=db_path)
    assert idx.recovered_from_corruption
    assert db_path.with_name("index.db.corrupt").exists()
    idx.add_file(_info("/ok/works.txt"))
    assert idx.file_exists("/ok/works.txt")
    idx.close()
    # and the recreated DB persists normally
    assert PersistentIndex(db_path=db_path).file_exists("/ok/works.txt")


def test_truncated_database_is_recovered(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_files_batch([_info(f"/t/file_{i}.txt") for i in range(500)])
    idx.close()
    data = db_path.read_bytes()
    db_path.write_bytes(data[: len(data) // 3] + b"\x00" * 50)
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            side.unlink()
    reopened = PersistentIndex(db_path=db_path)
    reopened.add_file(_info("/t/after.txt"))  # usable either way
    assert reopened.file_exists("/t/after.txt")
    reopened.close()


def test_migrates_legacy_v2_database(db_path):
    """A database written by the previous release keeps its data after upgrade."""
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE _schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            extension TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0, modified INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL DEFAULT 0, is_folder INTEGER NOT NULL DEFAULT 0, parent_path TEXT NOT NULL DEFAULT '',
            content_hash TEXT DEFAULT NULL, content_text TEXT DEFAULT NULL);
        CREATE TABLE trigrams (id INTEGER PRIMARY KEY AUTOINCREMENT, trigram TEXT NOT NULL UNIQUE);
        CREATE TABLE file_trigrams (file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            trigram_id INTEGER NOT NULL REFERENCES trigrams(id), PRIMARY KEY (file_id, trigram_id));
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO _schema_version (version) VALUES (1), (2);
        INSERT INTO files (path, name, content_text) VALUES ('/old/LegacyFile.TXT', 'LegacyFile.TXT', 'big text');
        INSERT INTO trigrams (trigram) VALUES ('leg'), ('ega'), ('ab');
        INSERT INTO file_trigrams VALUES (1, 1), (1, 2), (1, 3);
        """
    )
    conn.commit()
    conn.close()

    idx = PersistentIndex(db_path=db_path)
    assert [r["path"] for r in idx.search_trigram("legacyfile")] == ["/old/LegacyFile.TXT"] or \
        [r["path"] for r in idx.search_trigram("leg")] == ["/old/LegacyFile.TXT"]
    assert idx.get_file_by_path("/old/LegacyFile.TXT")["content_text"] is None
    assert idx._get_conn().execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == 3
    idx.close()


def test_newer_schema_is_set_aside_not_corrupted(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.close()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO _schema_version (version) VALUES (99)")
    conn.commit()
    conn.close()
    fresh = PersistentIndex(db_path=db_path)
    assert db_path.with_name("index.db.corrupt").exists()
    assert fresh.is_empty()
    fresh.close()


def test_close_closes_connections_of_all_threads_and_reopens_lazily(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_file(_info("/t/one.txt"))
    conns = []
    gate = threading.Event()

    def worker():
        conns.append(idx._get_conn())
        idx.file_exists("/t/one.txt")
        gate.wait(5)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    idx.close()
    gate.set()
    for t in threads:
        t.join()
    for c in conns:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")
    # still usable afterwards (connections are reopened on demand)
    assert idx.file_exists("/t/one.txt")
    idx.close()


def test_finished_threads_do_not_leak_connections(db_path):
    idx = PersistentIndex(db_path=db_path)

    def worker():
        idx.file_exists("/nothing")

    for _ in range(30):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    idx.file_exists("/nothing")  # triggers pruning on this thread's (re)connect path
    idx._get_conn()
    # force one more connection creation from a new thread to run the pruning
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(idx._readers) <= 3
    idx.close()


def test_concurrent_writers_and_readers(db_path):
    idx = PersistentIndex(db_path=db_path)
    errors = []

    def writer(n):
        try:
            for i in range(20):
                idx.add_files_batch([_info(f"/w{n}/file_{i}_{j}.txt") for j in range(50)])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            for _ in range(50):
                idx.search_trigram("file_1")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    ts = [threading.Thread(target=writer, args=(n,)) for n in range(4)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert idx.get_index_stats()["file_count"] == 4 * 20 * 50
    idx.close()


def test_rebuild_index_wipes_everything(db_path):
    idx = PersistentIndex(db_path=db_path)
    idx.add_file(_info("/r/a_file.txt"))
    idx.update_metadata("last_updated", "1")
    idx.rebuild_index()
    assert idx.is_empty()
    assert idx.get_metadata("last_updated") is None
    idx.close()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_windows_junction_loops_are_not_followed(tmp_path, db_path):
    import subprocess

    root = tmp_path / "r"
    _write(root / "real" / "f.txt")
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "real" / "loop"), str(root)],
        capture_output=True,
    )
    if res.returncode != 0:
        pytest.skip("cannot create junctions here")
    ix = _make_indexer(db_path)
    ix.incremental_update(str(root))  # must terminate
    paths = [r["path"] for r in ix.persistent_index.iter_all_files()]
    assert str(root / "real" / "loop") in paths  # listed as an entry...
    assert not any(p.startswith(str(root / "real" / "loop") + os.sep) for p in paths)  # ...but not entered
    ix.persistent_index.close()
