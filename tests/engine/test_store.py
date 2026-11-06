"""SQLite store v5 (design A.3) — ported v1 persistence/robustness behaviours plus op semantics."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from fast_file_search.engine.ops import DEL, DIR, DIRMTIME, DIRPATH, INS, REN, ROOTDEL, UPD, describe, validate
from fast_file_search.engine.protocol import FOLDER, UNREADABLE
from fast_file_search.engine.store import (DB_NAME, SCHEMA_VERSION, LoadChunk, RootRow, Store, StoreCorrupt,
                                           StoreTooNew, decode_db_text, encode_db_text, quarantine)

SEP = "\\" if os.name == "nt" else "/"
ROOT = ("C:\\data" if os.name == "nt" else "/data")


def j(*parts):
    return SEP.join(parts)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "cache" / DB_NAME


def small_tree(store: Store):
    rid = store.add_root(ROOT)
    a, b, c, d, e = store.allocate_ids(5)
    ops = [
        (INS, a, rid, "proj", 0, 10, 0, FOLDER), (DIR, a, rid, rid, j(ROOT, "proj"), 10),
        (INS, b, a, "main.py", 100, 20, 5, 0),
        (INS, c, rid, "proj2", 0, 10, 0, FOLDER), (DIR, c, rid, rid, j(ROOT, "proj2"), 10),
        (INS, d, c, "main.py", 200, 30, 6, 0),
        (INS, e, rid, "report_2024.pdf", 10, 40, 7, 0),
    ]
    store.apply(ops)
    return rid, (a, b, c, d, e)


def test_db_text_round_trip():
    for s in ("plain", "Café", "bad\udcffname", "\ud800\udc00two", "日本語", ""):
        enc = encode_db_text(s)
        assert decode_db_text(enc) == s
        enc.encode("utf-8")   # storable
    assert encode_db_text("Café") == "Café"


def test_ops_helpers():
    validate((INS, 1, 2, "a", 0, 0, 0, 0))
    with pytest.raises(ValueError):
        validate((INS, 1))
    with pytest.raises(ValueError):
        validate((99,))
    assert describe((DEL, 5)) == "DEL(5,)"


def test_schema_pragmas_and_meta(db):
    s = Store(db)
    c = s.conn
    assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA page_size").fetchone()[0] == 8192
    assert s.meta("schema") == "5" and s.generation == 0
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"meta", "roots", "dirs", "entries", "dirs_path", "dirs_parent", "entries_dir_name"} <= names
    assert not any("fts" in n or "trigram" in n for n in names)
    s.close()


def test_index_survives_reopen(db):
    s = Store(db)
    rid, ids = small_tree(s)
    gen = s.generation
    s.close()
    again = Store(db)
    assert again.generation == gen
    assert again.dir_lookup(j(ROOT, "proj")) == (ids[0], rid)
    assert again.entry_lookup(ids[0], "main.py") == (ids[1], 100, 20, 5, 0)
    assert again.roots() == [RootRow(rid, ROOT, "new", 0, 0, 0, 0, None, 0)]
    # ids keep increasing after a restart
    assert again.allocate_ids(1)[0] > max(ids)
    again.close()


def test_wal_and_no_data_loss_without_close(db):
    s = Store(db)
    rid, ids = small_tree(s)
    other = Store(db)   # a second handle on the same file sees committed data
    assert other.entry_lookup(rid, "report_2024.pdf") is not None
    other._conn.close()
    s._conn.close()      # crash: no close(), no checkpoint
    s._conn = None
    third = Store(db)
    assert third.entry_lookup(rid, "report_2024.pdf")[0] == ids[4]
    third.close()


def test_generation_bumps_once_per_apply_and_ids_are_monotonic(db):
    s = Store(db)
    g0 = s.generation
    r1 = s.allocate_ids(3)
    r2 = s.allocate_ids(2)
    assert list(r1) + list(r2) == list(range(r1.start, r2.stop))
    rid = s.add_root(ROOT)
    assert s.generation == g0 + 1
    assert rid >= r2.stop
    assert s.add_root(ROOT) == rid               # idempotent
    s.apply([])
    assert s.generation == g0 + 2
    s.close()
    s2 = Store(db)
    assert int(s2.meta("next_id")) > rid
    s2.close()


def test_children_and_lookups(db):
    s = Store(db)
    rid, (a, b, c, d, e) = small_tree(s)
    assert sorted(x[1] for x in s.children(rid)) == ["proj", "proj2", "report_2024.pdf"]
    assert s.children(a) == [(b, "main.py", 100, 20, 5, 0)]
    assert s.dir_lookup(j(ROOT, "nope")) is None
    assert s.entry_lookup(a, "nope") is None
    assert s.dir_row(a)[1:4] == (rid, rid, j(ROOT, "proj"))
    assert [x[0] for x in s.subdirs(rid)] == sorted([a, c])
    s.close()


def test_upd_ren_keep_ids(db):
    s = Store(db)
    rid, (a, b, c, d, e) = small_tree(s)
    s.apply([(UPD, b, 999, 50, 6, 0), (REN, e, a, "renamed.pdf")])
    assert s.entry_lookup(a, "main.py") == (b, 999, 50, 6, 0)
    assert s.entry_lookup(a, "renamed.pdf")[0] == e
    assert s.entry_lookup(rid, "report_2024.pdf") is None
    s.close()


def test_folder_rename_moves_subtree_paths(db):
    s = Store(db)
    rid, (a, b, c, d, e) = small_tree(s)
    sub = s.allocate_ids(2)
    s.apply([(INS, sub[0], a, "deep", 0, 0, 0, FOLDER), (DIR, sub[0], a, rid, j(ROOT, "proj", "deep"), 0),
             (INS, sub[1], sub[0], "x.txt", 1, 1, 1, 0)])
    s.apply([(REN, a, c, "moved"), (DIRPATH, a, j(ROOT, "proj2", "moved"))])
    assert s.dir_lookup(j(ROOT, "proj2", "moved")) == (a, rid)
    assert s.dir_lookup(j(ROOT, "proj2", "moved", "deep")) == (sub[0], rid)
    assert s.dir_lookup(j(ROOT, "proj")) is None
    assert s.dir_row(a)[1] == c                       # re-parented
    assert s.entry_lookup(sub[0], "x.txt")[0] == sub[1]
    s.close()


def test_removing_folder_removes_its_subtree_only(db):
    s = Store(db)
    rid, (a, b, c, d, e) = small_tree(s)
    sub = s.allocate_ids(2)
    s.apply([(INS, sub[0], a, "src", 0, 0, 0, FOLDER), (DIR, sub[0], a, rid, j(ROOT, "proj", "src"), 0),
             (INS, sub[1], sub[0], "deep.py", 1, 1, 1, 0)])
    s.apply([(DEL, a)])
    left = {(r[1]) for r in s.conn.execute("SELECT id, name FROM entries")}
    assert left == {"proj2", "main.py", "report_2024.pdf"}    # "proj2/main.py" is not under "proj"
    assert s.dir_lookup(j(ROOT, "proj")) is None and s.dir_lookup(j(ROOT, "proj", "src")) is None
    assert s.dir_lookup(j(ROOT, "proj2")) is not None
    s.close()


def test_delete_batch_beyond_sql_variable_limit(db):
    s = Store(db)
    rid = s.add_root(ROOT)
    ids = s.allocate_ids(3001)
    s.apply([(INS, i, rid, f"file_{i}.dat", 1, 1, 1, 0) for i in ids])
    s.apply([(DEL, i) for i in ids[:-1]])
    assert [x[1] for x in s.children(rid)] == [f"file_{ids[-1]}.dat"]
    s.close()


def test_dirmtime_and_rootdel(db):
    s = Store(db)
    rid, (a, b, c, d, e) = small_tree(s)
    s.apply([(DIRMTIME, a, 777, UNREADABLE)])
    assert s.dir_row(a)[4:] == (777, UNREADABLE)
    other = s.add_root(ROOT + "2")
    x = s.allocate_ids(1)[0]
    s.apply([(INS, x, other, "keep.txt", 1, 1, 1, 0)])
    s.apply([(ROOTDEL, rid)])
    assert [r.id for r in s.roots()] == [other]
    assert s.counts() == (1, 1)
    s.close()


def test_failed_batch_rolls_back_completely(db):
    s = Store(db)
    rid, (a, b, c, d, e) = small_tree(s)
    g = s.generation
    before = s.counts()
    x = s.allocate_ids(1)[0]
    with pytest.raises(sqlite3.IntegrityError):
        s.apply([(INS, x, rid, "fresh.txt", 1, 1, 1, 0), (INS, x + 1000, a, "main.py", 1, 1, 1, 0)])  # dup name
    assert s.counts() == before and s.generation == g
    assert s.entry_lookup(rid, "fresh.txt") is None
    s.apply([(INS, x, rid, "fresh.txt", 1, 1, 1, 0)])     # usable afterwards
    s.close()


def test_undecodable_and_unicode_names_round_trip(db):
    s = Store(db)
    rid = s.add_root(ROOT)
    names = ["bad\udcffname.txt", "\ud800lone", "Re\u0301sume\u0301.pdf", "Résumé.pdf", "日本語.txt"]
    ids = s.allocate_ids(len(names) + 1)
    weird_dir = j(ROOT, "dir\udcfe")
    ops = [(INS, ids[-1], rid, "dir\udcfe", 0, 0, 0, FOLDER), (DIR, ids[-1], rid, rid, weird_dir, 0)]
    ops += [(INS, i, ids[-1], n, 1, 1, 1, 0) for i, n in zip(ids, names)]
    s.apply(ops)
    s.close()
    s = Store(db)
    assert sorted(x[1] for x in s.children(ids[-1])) == sorted(names)   # NFD and NFC are distinct names
    assert s.dir_lookup(weird_dir) == (ids[-1], rid)
    assert s.entry_lookup(ids[-1], "bad\udcffname.txt")[0] == ids[0]
    loaded = [r for ch in s.iter_load() for r in ch.entries]
    assert {r[2] for r in loaded} == set(names) | {"dir\udcfe"}
    s.close()


def test_iter_load_chunks(db, monkeypatch):
    import fast_file_search.engine.store as st
    monkeypatch.setattr(st, "LOAD_CHUNK", 7)
    s = Store(db)
    rid = s.add_root(ROOT)
    ids = s.allocate_ids(20)
    s.apply([(INS, i, rid, f"f{i}", 1, 1, 1, 0) for i in ids])
    chunks = list(s.iter_load())
    assert all(isinstance(c, LoadChunk) for c in chunks)
    assert [len(c.entries) for c in chunks] == [7, 7, 6]
    assert chunks[0].roots and chunks[0].dirs and not chunks[1].dirs and not chunks[1].roots
    got = [r[0] for c in chunks for r in c.entries]
    assert got == sorted(got) == list(ids)
    s.close()
    empty = Store(db.with_name("empty.db"))
    chunks = list(empty.iter_load())
    assert len(chunks) == 1 and chunks[0].entries == []
    empty.close()


def test_set_root_state_is_written_with_the_next_batch(db):
    s = Store(db)
    rid = s.add_root(ROOT)
    s.set_root_state(rid, state="scanning", last_scan_start=5)
    assert s.roots()[0].state == "scanning"                   # visible at once
    raw = s.conn.execute("SELECT state FROM roots").fetchone()[0]
    assert raw == "new"                                       # not yet written
    s.apply([])
    assert s.conn.execute("SELECT state, last_scan_start FROM roots").fetchone() == ("scanning", 5)
    s.set_root_state(rid, state="ok", entries=10, last_scan_complete=1)
    s.close()                                                  # flushed at close
    s = Store(db)
    assert s.roots()[0][2:4] == ("ok", 10) and s.roots()[0].last_scan_complete == 1
    with pytest.raises(ValueError):
        s.set_root_state(rid, bogus=1)
    s.close()


def test_mark_open_and_clean_shutdown(db):
    s = Store(db)
    assert s.mark_open() is True          # fresh DB: previous shutdown counts as clean
    s.close(clean=False)
    s = Store(db)
    assert s.mark_open() is False         # unclean
    s.close()
    s = Store(db)
    assert s.mark_open() is True
    assert s.quick_check()
    s.close()


def test_corrupt_database_is_quarantined_and_recreated(db):
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is definitely not an sqlite database" * 100)
    with pytest.raises(StoreCorrupt):
        Store(db)
    s, recovered = Store.open_or_recover(db)
    assert recovered
    assert list(db.parent.glob(DB_NAME + ".corrupt-*"))
    rid = s.add_root(ROOT)
    s.close()
    s2, recovered = Store.open_or_recover(db)
    assert not recovered and s2.roots()[0].id == rid
    s2.close()


def test_truncated_database_is_recovered(db):
    s = Store(db)
    rid = s.add_root(ROOT)
    ids = s.allocate_ids(2000)
    s.apply([(INS, i, rid, f"file_{i}.txt", 1, 1, 1, 0) for i in ids])
    s.close()
    data = db.read_bytes()
    db.write_bytes(data[: len(data) // 3] + b"\x00" * 50)
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(str(db) + suffix)
        except OSError:
            pass
    s2, recovered = Store.open_or_recover(db)   # usable either way
    r2 = s2.add_root(ROOT + "x")
    assert any(r.id == r2 for r in s2.roots())
    s2.close()


def test_unclean_shutdown_runs_quick_check(db, monkeypatch):
    s = Store(db)
    s.add_root(ROOT)
    s.mark_open()
    s.close(clean=False)
    monkeypatch.setattr(Store, "quick_check", lambda self: False)
    s2, recovered = Store.open_or_recover(db)
    assert recovered and s2.roots() == []
    s2.close()


def test_newer_schema_is_set_aside_not_corrupted(db):
    s = Store(db)
    s.add_root(ROOT)
    s.close()
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version=99")
    c.commit()
    c.close()
    with pytest.raises(StoreTooNew):
        Store(db)
    fresh, recovered = Store.open_or_recover(db)
    assert recovered and fresh.roots() == []
    assert list(db.parent.glob(DB_NAME + ".corrupt-*"))
    fresh.close()


def test_older_or_foreign_layout_is_rebuilt(db):
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    c.execute("PRAGMA user_version=4")
    c.commit()
    c.close()
    s = Store(db)
    names = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "files" not in names and "entries" in names
    s.close()


def test_quarantine_keeps_newest_two(tmp_path):
    p = tmp_path / DB_NAME
    for i in range(4):
        p.write_bytes(b"x")
        quarantine(p)
        time.sleep(0.002)
    assert len(list(tmp_path.glob(DB_NAME + ".corrupt-*"))) == 2
    assert not p.exists()


def test_remove_legacy_v4_files(tmp_path):
    for n in ("index.db", "index.db-wal", "index.db-shm", "index.db.corrupt", "index.db.123.corrupt",
              "search_history.db", "index-v5.db"):
        (tmp_path / n).write_bytes(b"x")
    removed = Store.remove_legacy(tmp_path)
    assert len(removed) == 5
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index-v5.db", "search_history.db"]


def test_bulk_insert_is_one_fast_transaction(db):
    s = Store(db)
    rid = s.add_root(ROOT)
    dirs = s.allocate_ids(50)
    s.apply([op for k, d in enumerate(dirs) for op in ((INS, d, rid, f"dir{k}", 0, 0, 0, FOLDER),
                                                       (DIR, d, rid, rid, j(ROOT, f"dir{k}"), 0))])
    ids = s.allocate_ids(20_000)
    ops = [(INS, i, dirs[k % 50], f"some_file_name_{k}.txt", k, 1_700_000_000, 0, 0) for k, i in enumerate(ids)]
    g = s.generation
    t0 = time.perf_counter()
    s.apply(ops)
    dt = time.perf_counter() - t0
    assert s.generation == g + 1
    assert dt < 2.0          # acceptance (<= 120 ms) is measured by bench_local.py
    assert s.counts()[0] == 20_050
    s.close()
