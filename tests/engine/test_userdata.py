"""User data (design A.8): v1 history semantics, favorites/tags/notes, migration from a real v1
``search_history.db`` (made with the v1 SearchHistory code), backup and restore."""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from fast_file_search.engine.userdata import HistoryItem, UserData, matches_prefix

FIXTURE = Path(__file__).with_name("fixtures") / "search_history_v1.db"


@pytest.fixture
def ud(tmp_path):
    u = UserData(tmp_path / "search_history.db")
    yield u
    u.close()


def test_history_dedupes_and_bounds_size(ud):
    ud.record_search("alpha", 1)
    ud.record_search("beta", 2)
    ud.record_search("alpha", 3)
    ud.record_search("   ", 9)       # ignored
    ud.record_search("", 9)          # ignored
    items = ud.history()
    assert [h.query for h in items] == ["alpha", "beta"]
    assert items[0].count == 3 and isinstance(items[0], HistoryItem)
    assert dict(ud.frequent_queries())["alpha"] == 2
    for i in range(UserData.MAX_ENTRIES + 150):
        ud.record_search(f"q{i}", i)
    total = ud._conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    assert total == UserData.MAX_ENTRIES
    assert ud.history(limit=1)[0].query == f"q{UserData.MAX_ENTRIES + 149}"   # newest first, equal timestamps ok
    assert len(ud.history(limit=1000)) == UserData.RECENT                     # in-memory distinct list


def test_history_survives_reopen(tmp_path):
    p = tmp_path / "h.db"
    u = UserData(p)
    u.record_search("keep me", 4)
    u.close()
    u2 = UserData(p)
    assert [h.query for h in u2.history()] == ["keep me"]
    u2.close()


def test_pins_first_and_persisted(tmp_path):
    p = tmp_path / "h.db"
    u = UserData(p)
    u.record_search("older", 1)
    u.record_search("newer", 2)
    u.pin("older")
    u.pin("never ran")
    items = u.history()
    assert [(h.query, h.pinned) for h in items] == [("never ran", True), ("older", True), ("newer", False)]
    assert items[1].count == 1 and items[0].count == 0
    u.unpin("never ran")
    assert [h.query for h in u.history()] == ["older", "newer"]
    u.clear_history()                                  # pins survive clearing history
    assert [h.query for h in u.history()] == ["older"]
    u.close()
    u2 = UserData(p)
    assert [(h.query, h.pinned) for h in u2.history()] == [("older", True)]
    u2.close()


def test_history_prefix_rule():
    assert matches_prefix("Annual Report", "ann")
    assert matches_prefix("annual report", "")
    assert matches_prefix("Résumé 2024", "resu")
    assert not matches_prefix("annual report", "re")         # < 3 chars: prefix only
    assert matches_prefix("annual report", "rep")            # >= 3 chars: substring
    assert not matches_prefix("abc", "abcd")


def test_history_prefix_filtering(ud):
    for q in ("invoice 2024", "report", "my invoices", "inv"):
        ud.record_search(q, 1)
    assert [h.query for h in ud.history("inv")] == ["inv", "my invoices", "invoice 2024"]
    assert [h.query for h in ud.history("in")] == ["inv", "invoice 2024"]


def test_favorites_tags_notes(ud):
    p = "/docs/file.txt"
    assert not ud.is_favorite(p) and ud.favorite_paths() == frozenset()
    g = ud.favorites_generation
    ud.set_favorite(p, True)
    assert ud.is_favorite(p) and ud.favorites_generation == g + 1
    assert ud.favorite_paths() == frozenset({p})
    assert ud.favorite_paths() is ud.favorite_paths()        # cached until changed
    ud.set_favorite(p, False)
    assert not ud.is_favorite(p) and ud.favorites_generation == g + 2
    ud.set_note(p, "Important document")
    assert ud.note(p) == "Important document"
    ud.set_note(p, "  ")
    assert ud.note(p) == ""
    ud.set_tags(p, ["Work", "Finance", "Work", " "])
    assert ud.tags(p) == ["Finance", "Work"]
    ud.set_tags(p, ["Finance"])
    assert ud.tags(p) == ["Finance"]
    weird = "/odd/bad\udcffname.txt"
    ud.set_favorite(weird, True)
    ud.set_tags(weird, ["x"])
    assert weird in ud.favorite_paths() and ud.tags(weird) == ["x"]


def test_forget_keeps_user_data(ud):
    ud.set_favorite("/a/x", True)
    ud.set_note("/a/x", "n")
    ud.record_search("x", 1)
    ud.forget(["/a/x"])
    assert ud.is_favorite("/a/x") and ud.note("/a/x") == "n" and ud.history()[0].query == "x"


def test_migration_from_v1_file_loses_nothing(tmp_path):
    p = tmp_path / "search_history.db"
    shutil.copyfile(FIXTURE, p)
    c = sqlite3.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0     # written by v1
    c.close()
    u = UserData(p)
    assert u.recovered is None
    assert u._conn.execute("PRAGMA user_version").fetchone()[0] == 1
    items = u.history()
    assert [(h.query, h.pinned) for h in items] == [
        ("never run pin", True), ("config files", True), ("日本語", False), ("ext:pdf invoice", False),
        ("résumé", False), ("annual report", False)]
    assert {h.query: h.count for h in items}["annual report"] == 17
    assert u.favorite_paths() == frozenset({"/docs/file.txt", "C:\\Users\\me\\Café.docx", "/odd/bad\udcffname.txt"})
    assert u.tags("/docs/file.txt") == ["Finance", "Work"]
    assert u.tags("/odd/bad\udcffname.txt") == ["weird"]
    assert u.note("/docs/file.txt") == "Important document"
    assert u.note("C:\\Users\\me\\Café.docx") == "CV \u2014 keep \u2713"
    u.record_search("new one", 1)
    u.close()
    # and the v1 tables are untouched in shape (a v1 build could still read them)
    c = sqlite3.connect(p)
    cols = [r[1] for r in c.execute("PRAGMA table_info(search_history)")]
    assert cols == ["id", "query", "timestamp", "result_count", "pinned"]
    c.close()


def test_corrupt_file_without_backup_is_reset(tmp_path):
    p = tmp_path / "h.db"
    p.write_bytes(b"this is definitely not an sqlite database" * 50)
    u = UserData(p)                               # must not raise
    assert u.recovered == "reset"
    u.record_search("works again", 1)
    assert [h.query for h in u.history()] == ["works again"]
    u.close()
    assert (tmp_path / "h.db.corrupt").exists()


def test_backup_and_restore(tmp_path):
    p = tmp_path / "h.db"
    u = UserData(p)
    u.record_search("before backup", 1)
    u.set_favorite("/f", True)
    assert u.backup_if_due() is True
    assert (tmp_path / "h.db.bak").exists()
    assert u.backup_if_due() is False            # not due again within a day
    u.record_search("after backup", 2)
    old = time.time() - 2 * 86400
    os.utime(tmp_path / "h.db.bak", (old, old))
    assert u.backup_if_due() is True             # rotates .bak -> .bak.1
    assert (tmp_path / "h.db.bak.1").exists()
    u.close()
    p.write_bytes(b"garbage" * 1000)
    for side in ("-wal", "-shm"):
        try:
            os.remove(str(p) + side)
        except OSError:
            pass
    u2 = UserData(p)
    assert u2.recovered == "restored"
    assert [h.query for h in u2.history()] == ["after backup", "before backup"]
    assert u2.is_favorite("/f")
    assert (tmp_path / "h.db.corrupt").exists()
    u2.close()


def test_falls_back_to_memory_when_location_unusable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    u = UserData(blocker / "sub" / "h.db")       # parent is a file -> mkdir fails
    assert u.recovered == "memory"
    u.record_search("still ok", 1)
    u.set_favorite("/x", True)
    assert u.history()[0].query == "still ok" and u.is_favorite("/x")
    assert u.backup_if_due() is False
    u.close()


def test_corruption_during_use_is_recovered(tmp_path, monkeypatch):
    p = tmp_path / "h.db"
    u = UserData(p)
    u.record_search("one", 1)
    calls = {"n": 0}
    real = u._conn

    class Broken:
        def execute(self, *a, **k):
            calls["n"] += 1
            raise sqlite3.DatabaseError("database disk image is malformed")

        def commit(self):
            pass

        def close(self):
            real.close()

    u._conn = Broken()
    u.record_search("two", 2)                    # recovered and retried
    assert calls["n"] == 1
    assert "two" in [h.query for h in u.history()]
    u.close()
