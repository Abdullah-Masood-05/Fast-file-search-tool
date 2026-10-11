"""Per-platform behaviour of the core (dirs, path rules, excludes, Unicode
names, desktop helpers, watcher degradation). Platform branches are exercised
by monkeypatching ``sys.platform`` / env vars and mocking subprocess calls;
tests that need a real file system feature skip where the OS lacks it."""
import errno
import logging
import os
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path, PurePosixPath

import pytest

from fast_file_search import file_watcher as fw
from fast_file_search import indexer as indexer_mod
from fast_file_search.config import AppConfig
from fast_file_search.file_watcher import FileWatcher
from fast_file_search.indexer import Indexer, root_excludes, system_folder_names
from fast_file_search.persistent_index import PersistentIndex, decode_db_text, encode_db_text
from fast_file_search.search import SearchEngine
from fast_file_search.search_history import SearchHistory
from fast_file_search.utils import file_utils
from fast_file_search.utils import platform_utils as pu
from fast_file_search.utils.file_utils import ExcludeMatcher, nfc, search_key

IS_WINDOWS = sys.platform == "win32"
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX file system semantics")
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows only")

NFC_NAME = unicodedata.normalize("NFC", "Café Résumé.txt")
NFD_NAME = unicodedata.normalize("NFD", "Café Résumé.txt")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for var in ("FAST_FILE_SEARCH_CACHE_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def index(tmp_path):
    idx = PersistentIndex(db_path=tmp_path / "db" / "index.db")
    yield idx
    idx.close()


# --------------------------------------------------------------------------- per-user dirs

def test_linux_dirs_follow_xdg(fake_home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert pu.get_app_data_dir("App") == fake_home / ".config" / "App"
    assert pu.get_cache_dir("App") == fake_home / ".cache" / "App"
    assert pu.get_data_dir("App") == fake_home / ".local" / "share" / "App"

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg / "data"))
    assert pu.get_app_data_dir("App") == xdg / "cfg" / "App"
    assert pu.get_cache_dir("App") == xdg / "cache" / "App"
    assert pu.get_data_dir("App") == xdg / "data" / "App"

    # Empty or relative values must be ignored (XDG spec)
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/dir")
    assert pu.get_app_data_dir("App") == fake_home / ".config" / "App"
    assert pu.get_cache_dir("App") == fake_home / ".cache" / "App"


def test_macos_dirs(fake_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    support = fake_home / "Library" / "Application Support"
    assert pu.get_app_data_dir("App") == support / "App"
    assert pu.get_cache_dir("App") == fake_home / "Library" / "Caches" / "App"
    assert pu.get_data_dir("App") == support / "App"


def test_windows_dirs_unchanged(fake_home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert pu.get_app_data_dir("App") == tmp_path / "Roaming" / "App"
    assert pu.get_cache_dir("App") == tmp_path / "Local" / "App"
    # existing Windows history/favorites stay where they were
    assert pu.get_data_dir("App") == pu.get_cache_dir("App")


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_cache_dir_override_wins_everywhere(fake_home, tmp_path, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("FAST_FILE_SEARCH_CACHE_DIR", str(tmp_path / "portable"))
    assert pu.get_cache_dir("App") == tmp_path / "portable" / "App"
    assert pu.get_data_dir("App") == tmp_path / "portable" / "App"


def test_default_index_root_is_home_not_cwd(fake_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pu.get_default_index_root() == str(fake_home)


def test_history_moves_from_cache_to_data_dir(fake_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    old = pu.get_cache_dir("Fast File Search Pro") / "search_history.db"
    h = SearchHistory(db_path=old)
    h.add_favorite("/home/me/fav.txt")
    h.close()

    h = SearchHistory()
    try:
        assert h.db_path == fake_home / ".local" / "share" / "Fast File Search Pro" / "search_history.db"
        assert h.get_favorites() == ["/home/me/fav.txt"]
        assert not old.exists()
    finally:
        h.close()


# --------------------------------------------------------------------------- path rules

def test_windows_paths_case_insensitive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert pu.paths_equal("C:\\Users\\Me\\Docs", "c:/users/me/docs/")
    assert pu.is_path_under("c:\\USERS\\me\\docs\\a.txt", "C:\\Users\\Me")
    assert not pu.is_path_under("C:\\Users\\Mex", "C:\\Users\\Me")
    assert pu.is_path_under("D:\\x", "D:\\")


def test_macos_paths_case_and_normalization_insensitive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pu.paths_equal("/Users/me/" + NFD_NAME, "/users/ME/" + NFC_NAME.upper())
    assert pu.is_path_under("/Users/Me/Docs/x", "/users/me/docs")


def test_linux_paths_case_sensitive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert not pu.paths_equal("/home/A", "/home/a")
    assert not pu.paths_equal("/home/" + NFC_NAME, "/home/" + NFD_NAME)
    assert pu.is_path_under("/a/b/c", "/a/b")
    assert not pu.is_path_under("/a/b\\c", "/a/b")  # backslash is a name character
    assert pu.is_path_under("/anything", "/")


def test_dedupe_roots(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert pu.dedupe_roots(["C:\\Data\\sub", "c:\\data", "C:\\DATA\\", "D:\\x", ""]) == ["c:\\data", "D:\\x"]
    monkeypatch.setattr(sys, "platform", "linux")
    assert pu.dedupe_roots(["/home/A", "/home/a", "/home/a/b"]) == ["/home/A", "/home/a"]


def test_config_dedupes_case_variant_roots(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = AppConfig(index_paths=["C:\\Data", "c:/data/", "", "D:\\x"])
    assert cfg.index_paths == ["C:\\Data", "D:\\x"]
    monkeypatch.setattr(sys, "platform", "linux")
    assert AppConfig(index_paths=["/data", "/Data"]).index_paths == ["/data", "/Data"]


def test_exclude_matcher_case_rules(monkeypatch):
    monkeypatch.setattr(file_utils, "_IS_CASE_INSENSITIVE", False)
    m = ExcludeMatcher(["node_modules", "*.TMP"])
    assert m.matches_name("node_modules") and not m.matches_name("Node_Modules")
    assert not m.matches_name("x.tmp")

    monkeypatch.setattr(file_utils, "_IS_CASE_INSENSITIVE", True)
    m = ExcludeMatcher(["node_modules", "*.TMP", NFC_NAME])
    assert m.matches_name("Node_Modules") and m.matches_name("x.tmp")
    assert m.matches_name(NFD_NAME)  # macOS hands out decomposed names


@posix_only
def test_backslash_is_not_a_separator_on_posix():
    m = ExcludeMatcher([".git"])
    assert not m.matches_path("/repo/notes\\.git")
    assert m.matches_path("/repo/.git/config")


@windows_only
def test_backslash_is_a_separator_on_windows():
    assert ExcludeMatcher([".git"]).matches_path("C:\\repo\\.git\\config")


# --------------------------------------------------------------------------- default excludes

def test_system_folder_names_per_os():
    win, mac, linux = (system_folder_names(p) for p in ("win32", "darwin", "linux"))
    assert {"Windows", "Program Files", "$Recycle.Bin"} <= win
    assert "Windows" not in linux and "Program Files" not in mac
    assert {".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100", ".DS_Store",
            ".Trash", "Backups.backupdb"} <= mac
    assert root_excludes("linux") == {"/proc", "/sys", "/dev", "/run", "/snap", "/tmp"}
    assert {"/System", "/Volumes", "/private/var", "/dev"} <= root_excludes("darwin")
    assert root_excludes("win32") == set()


def test_root_excludes_only_apply_at_their_absolute_path(monkeypatch, index):
    monkeypatch.setattr(indexer_mod, "_ROOT_EXCLUDES", root_excludes("linux"))
    monkeypatch.setattr(indexer_mod, "_ROOT_EXCLUDES_KEYS", root_excludes("linux"))
    monkeypatch.setattr(indexer_mod, "_PATHS_FOLD_CASE", False)
    ix = Indexer(config=AppConfig(exclude_patterns=[]), persistent_index=index)
    assert ix._name_skipped("tmp", "/")
    assert ix._name_skipped("proc", "/")
    assert not ix._name_skipped("tmp", "/home/me/project")  # a project's tmp/ is data
    assert not ix._name_skipped("Windows", "/home/me") or IS_WINDOWS


def _paths_of(index):
    return {r["path"] for r in index.iter_all_files()}


@posix_only
def test_walk_does_not_follow_symlinks_or_loop(tmp_path, index):
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f.txt").write_text("x")
    os.symlink(root, root / "a" / "b" / "loop")  # would recurse forever if followed
    os.symlink(root / "a" / "b" / "f.txt", root / "link.txt")
    stats = Indexer(config=AppConfig(), persistent_index=index).incremental_update(str(root))
    paths = _paths_of(index)
    assert str(root / "a" / "b" / "loop") in paths  # listed as an entry...
    assert not any("loop" + os.sep in p for p in paths)  # ...but never entered
    assert str(root / "link.txt") in paths
    assert stats.errors == 0


@posix_only
def test_walk_does_not_cross_mount_points(tmp_path, index, monkeypatch):
    root = tmp_path / "root"
    (root / "mnt").mkdir(parents=True)
    (root / "mnt" / "inside.txt").write_text("x")
    (root / "top.txt").write_text("x")
    # Pretend everything below the root lives on another device.
    monkeypatch.setattr(indexer_mod, "_same_volume_devices", lambda r: {-1})
    Indexer(config=AppConfig(), persistent_index=index).incremental_update(str(root))
    paths = _paths_of(index)
    assert str(root / "mnt") in paths and str(root / "top.txt") in paths
    assert str(root / "mnt" / "inside.txt") not in paths


@windows_only
def test_walk_does_not_enter_junctions(tmp_path, index):
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / "f.txt").write_text("x")
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "junc"), str(root)],
                          capture_output=True)
    if proc.returncode != 0:
        pytest.skip("cannot create a junction here")
    Indexer(config=AppConfig(), persistent_index=index).incremental_update(str(root))
    paths = _paths_of(index)
    assert str(root / "junc") in paths
    assert not any(p.startswith(str(root / "junc") + "\\") for p in paths)


# --------------------------------------------------------------------------- unicode names

def test_nfd_names_match_nfc_queries_and_vice_versa(index):
    index.add_files_batch([
        {"path": "/u/" + NFD_NAME, "name": NFD_NAME, "size": 1},
        {"path": "/u/plain.txt", "name": "plain.txt", "size": 1},
    ])
    for query in (unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café"),
                  unicodedata.normalize("NFC", "RÉSUMÉ")):
        assert [r["path"] for r in index.search_trigram(query)] == ["/u/" + NFD_NAME], query
    assert [r["path"] for r in index.search_trigram(unicodedata.normalize("NFC", "é"))] == ["/u/" + NFD_NAME]

    engine = SearchEngine(persistent_index=index)
    resp = engine.search_persistent(unicodedata.normalize("NFC", "Café"))
    assert [r.path for r in resp.results] == ["/u/" + NFD_NAME]
    resp = engine.search_persistent("name:" + unicodedata.normalize("NFC", "résumé"))
    assert [r.path for r in resp.results] == ["/u/" + NFD_NAME]


def test_migration_rekeys_existing_nfd_rows(tmp_path):
    db = tmp_path / "index.db"
    idx = PersistentIndex(db_path=db)
    idx.add_file({"path": "/u/" + NFD_NAME, "name": NFD_NAME})
    conn = idx._get_writer()
    # Simulate a v3 database written before names were NFC-normalized.
    conn.execute("UPDATE files SET name_lower = ?", (NFD_NAME.lower(),))
    conn.execute("DELETE FROM file_trigrams")
    conn.execute("DELETE FROM _schema_version WHERE version = 4")
    idx.close()

    idx = PersistentIndex(db_path=db)
    try:
        assert [r["name"] for r in idx.search_trigram(nfc("café"))] == [NFD_NAME]
    finally:
        idx.close()


SURROGATE_NAME = os.fsdecode(b"caf\xe9 report.txt") if not IS_WINDOWS else "caf\udce9 report.txt"


def test_undecodable_names_round_trip_through_sqlite(index):
    path = "/data/" + SURROGATE_NAME
    assert encode_db_text(path) != path and decode_db_text(encode_db_text(path)) == path
    assert encode_db_text("/plain/é") == "/plain/é"
    index.add_files_batch([
        {"path": path, "name": SURROGATE_NAME, "parent_path": "/data"},
        {"path": "/data/ok.txt", "name": "ok.txt", "parent_path": "/data"},
    ])
    assert index.file_exists(path)
    assert [r["path"] for r in index.search_trigram("report")] == [path]
    assert index.get_file_by_path(path)["name"] == SURROGATE_NAME
    assert {p for p, *_ in index.iter_file_states("/data")} == {path, "/data/ok.txt"}
    index.remove_file(path)
    assert not index.file_exists(path) and index.file_exists("/data/ok.txt")


def test_undecodable_paths_in_history(tmp_path):
    h = SearchHistory(db_path=tmp_path / "h.db")
    try:
        path = "/data/" + SURROGATE_NAME
        h.add_favorite(path)
        h.add_tag(path, "t")
        h.set_note(path, "n")
        assert h.get_favorites() == [path] and h.is_favorite(path)
        assert h.get_tags(path) == ["t"] and h.get_note(path) == "n"
    finally:
        h.close()


def test_search_key_is_valid_utf8():
    key = search_key(SURROGATE_NAME)
    key.encode("utf-8")  # must not raise
    assert "report" in key


def _make_odd_files(root: Path):
    """Create names the OS allows that are hard to store; return created names."""
    made = []
    candidates = []
    if IS_WINDOWS:
        candidates.append("lone\ud800surrogate.txt")
    else:
        candidates += [os.fsdecode(b"latin\xe9.txt"), "new\nline.txt", "back\\slash.txt"]
    for name in candidates:
        try:
            with open(os.path.join(str(root), name), "w") as fh:
                fh.write("x")
            if name in os.listdir(str(root)):
                made.append(name)
        except (OSError, UnicodeError, ValueError):
            continue
    return made


def test_indexing_odd_names_does_not_lose_files(tmp_path, index):
    root = tmp_path / "odd"
    root.mkdir()
    odd = _make_odd_files(root)
    if not odd:
        pytest.skip("this file system rejects every odd name")
    for i in range(5):
        (root / f"good{i}.txt").write_text("x")
    ix = Indexer(config=AppConfig(), persistent_index=index)
    stats = ix.incremental_update(str(root))
    assert stats.errors == 0
    paths = _paths_of(index)
    for name in odd + [f"good{i}.txt" for i in range(5)]:
        p = os.path.join(str(root), name)
        assert p in paths, repr(name)
        assert os.path.lexists(p)  # stored path still opens the file
    again = ix.incremental_update(str(root))  # no churn: everything unchanged
    assert again.removed == 0 and again.added == 0 and again.unchanged == len(odd) + 5


def test_linux_descendant_clause_ignores_backslash_siblings(index, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    index.add_files_batch([
        {"path": "/a/b", "name": "b", "is_folder": True},
        {"path": "/a/b/c", "name": "c"},
        {"path": "/a/b\\c", "name": "b\\c"},
    ])
    index.remove_file("/a/b")
    assert _paths_of(index) == {"/a/b\\c"}


# --------------------------------------------------------------------------- desktop helpers

class _Recorder:
    def __init__(self, returncode=0, stdout=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout

    def run(self, cmd, *args, **kwargs):
        self.calls.append(cmd if isinstance(cmd, str) else list(cmd))
        rc = self.returncode(cmd) if callable(self.returncode) else self.returncode
        out = self.stdout(cmd) if callable(self.stdout) else self.stdout
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    def popen(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))

        class _P:
            returncode = 0

            def communicate(self, *a, **k):
                return (b"", b"")
        return _P()


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(pu.subprocess, "run", rec.run)
    monkeypatch.setattr(pu.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(pu, "_qt_app", lambda: None)
    return rec


def test_reveal_windows(tmp_path, recorder, monkeypatch):
    f = tmp_path / "a,b c.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "win32")
    pu.open_file_manager(f)
    # one quoted token, so commas/spaces in the path cannot split it
    assert recorder.calls == [f'explorer /select,"{f.absolute()}"']


def test_reveal_macos(tmp_path, recorder, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pu.open_file_manager(f) is True
    assert recorder.calls == [["open", "-R", str(f.absolute())]]


def test_reveal_linux_uses_dbus_show_items(tmp_path, recorder, monkeypatch):
    f = tmp_path / "it's, odd.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu.shutil, "which", lambda name: "/usr/bin/" + name)
    assert pu.open_file_manager(f) is True
    cmd = recorder.calls[0]
    assert cmd[0] == "gdbus" and "org.freedesktop.FileManager1.ShowItems" in cmd
    uri_arg = cmd[-2]
    assert cmd[-1] == "''"  # empty startup id in GVariant text syntax
    assert uri_arg.startswith("['file://") and uri_arg.endswith("']")
    assert "'" not in uri_arg[2:-2] and "," not in uri_arg  # quote/comma are escaped


def test_reveal_linux_falls_back_to_xdg_open(tmp_path, recorder, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu.shutil, "which", lambda name: "/usr/bin/" + name)
    recorder.returncode = 1  # no FileManager1 service on the bus
    assert pu.open_file_manager(f) is True
    assert [c[0] for c in recorder.calls] == ["gdbus", "dbus-send", "xdg-open"]
    assert recorder.calls[-1] == ["xdg-open", str(f.absolute().parent)]


def test_reveal_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        pu.open_file_manager(tmp_path / "missing")


def test_file_uri_quoting():
    assert pu._file_uri(PurePosixPath("/tmp/it's a,b.txt")) == "file:///tmp/it%27s%20a%2Cb.txt"
    assert pu._file_uri(PurePosixPath("/tmp/caf\u00e9")) == "file:///tmp/caf%C3%A9"


@pytest.mark.parametrize("platform,expected", [("darwin", ["open"]), ("linux", ["xdg-open"])])
def test_open_path_posix(tmp_path, recorder, monkeypatch, platform, expected):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", platform)
    assert pu.open_path(f) is True
    assert recorder.calls == [expected + [str(f)]]


def test_open_path_windows(tmp_path, recorder, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("x")
    opened = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(pu.os, "startfile", opened.append, raising=False)
    assert pu.open_path(f) is True and opened == [str(f)]


def test_move_to_trash_linux_fallback(tmp_path, recorder, monkeypatch):
    from PySide6.QtCore import QFile

    f = tmp_path / "doomed.txt"
    f.write_text("x")
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda *a: (False, "")))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu.shutil, "which", lambda name: "/usr/bin/" + name if name == "gio" else None)

    def fake_run(cmd, *a, **k):
        recorder.calls.append(list(cmd))
        os.remove(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(pu.subprocess, "run", fake_run)
    assert pu.move_to_trash(f) is True
    assert recorder.calls == [["gio", "trash", "--", str(f)]]
    assert pu.move_to_trash(tmp_path / "missing") is False


def test_move_to_trash_reports_failure(tmp_path, recorder, monkeypatch):
    from PySide6.QtCore import QFile

    f = tmp_path / "kept.txt"
    f.write_text("x")
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda *a: (False, "")))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu.shutil, "which", lambda name: None)
    assert pu.move_to_trash(f) is False and f.exists()  # never deletes permanently


def test_theme_macos(recorder, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    recorder.stdout = "Dark\n"
    assert pu.get_system_theme() == "dark"
    assert recorder.calls[-1] == ["defaults", "read", "-g", "AppleInterfaceStyle"]
    recorder.returncode, recorder.stdout = 1, ""  # key absent in light mode
    assert pu.get_system_theme() == "light"


@pytest.mark.parametrize("scheme,gtk,expected", [
    ("'prefer-dark'", "'Adwaita'", "dark"),
    ("'prefer-light'", "'Adwaita-dark'", "light"),
    ("'default'", "'Adwaita-dark'", "dark"),
    ("'default'", "'Yaru'", "light"),
])
def test_theme_linux_gsettings(recorder, monkeypatch, scheme, gtk, expected):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("GTK_THEME", raising=False)
    recorder.stdout = lambda cmd: scheme if cmd[-1] == "color-scheme" else gtk
    assert pu.get_system_theme() == expected


def test_theme_linux_without_gsettings(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu, "_qt_app", lambda: None)
    monkeypatch.delenv("GTK_THEME", raising=False)

    def missing(*a, **k):
        raise FileNotFoundError("gsettings")
    monkeypatch.setattr(pu.subprocess, "run", missing)
    assert pu.get_system_theme() == "light"
    monkeypatch.setenv("GTK_THEME", "Adwaita:dark")
    assert pu.get_system_theme() == "dark"


def test_clipboard_prefers_qt(monkeypatch):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def no_subprocess(*a, **k):
        raise AssertionError("subprocess used although Qt is available")
    monkeypatch.setattr(pu.subprocess, "run", no_subprocess)
    monkeypatch.setattr(pu.subprocess, "Popen", no_subprocess)
    assert pu.copy_to_clipboard("hello path") is True
    assert app.clipboard().text() == "hello path"


# --------------------------------------------------------------------------- file watcher

def test_watcher_maps_event_paths_to_root_spelling(index, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    w = FileWatcher(persistent_index=index, config=AppConfig())
    w._watched_paths = {"/var/folders/xy/root"}
    w._aliases = [("/private/var/folders/xy/root", "/var/folders/xy/root")]
    assert w._normalize_event_path("/private/var/folders/xy/root/a/b.txt") == \
        os.path.join("/var/folders/xy/root", "a", "b.txt")
    # different case of the configured root on a case-insensitive volume
    assert w._normalize_event_path("/VAR/folders/XY/root/c.txt") == \
        os.path.join("/var/folders/xy/root", "c.txt")
    assert w._normalize_event_path("/elsewhere/x") == "/elsewhere/x"


def test_watcher_judges_excludes_below_root_with_case_rules(index, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    w = FileWatcher(persistent_index=index, config=AppConfig(exclude_patterns=["projects"]))
    w._watched_paths = {"/Users/me/Projects/app"}
    assert not w._is_ignored("/users/me/projects/app/main.py")
    assert w._is_ignored("/Users/me/Projects/app/projects/x.py")


def test_watcher_set_paths_dedupes(tmp_path, index):
    (tmp_path / "a" / "b").mkdir(parents=True)
    w = FileWatcher(persistent_index=index, config=AppConfig())
    w.set_paths([str(tmp_path / "a" / "b"), str(tmp_path / "a"), str(tmp_path / "missing")])
    assert w.watched_paths == [str(tmp_path / "a")]


class _FakeObserver:
    instances = []
    fail_for = set()

    def __init__(self):
        self.scheduled = []
        self.started = self.stopped = False
        _FakeObserver.instances.append(self)

    def start(self):
        self.started = True

    def schedule(self, handler, path, recursive=False):
        if path in self.fail_for:
            raise OSError(errno.ENOSPC, "inotify watch limit reached")
        self.scheduled.append(path)

    def stop(self):
        self.stopped = True

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


@pytest.mark.parametrize("fail_all", [False, True])
def test_watcher_degrades_on_inotify_limits(tmp_path, index, monkeypatch, caplog, fail_all):
    roots = [tmp_path / "r1", tmp_path / "r2", tmp_path / "r3"]
    for r in roots:
        r.mkdir()
    _FakeObserver.instances = []
    _FakeObserver.fail_for = {str(r) for r in (roots if fail_all else roots[:2])}
    monkeypatch.setattr(fw, "Observer", _FakeObserver)
    errors = []
    w = FileWatcher(persistent_index=index, config=AppConfig())
    w.on_error(errors.append)
    w.set_paths([str(r) for r in roots])
    with caplog.at_level(logging.WARNING, logger=fw.__name__):
        w.start()
    obs = _FakeObserver.instances[-1]
    assert len(errors) == 1 and errors[0].errno == errno.ENOSPC  # reported once
    assert sum("Live file watching disabled" in r.message for r in caplog.records) == 1
    if fail_all:
        assert not w.is_running and obs.stopped
    else:
        assert w.is_running and obs.scheduled == [str(roots[2])]
        w.stop()


def test_close_leaked_inotify_closes_fds():
    r, wfd = os.pipe()

    class Inotify:  # mimics watchdog's object left half-initialised
        def __init__(self):
            self._inotify_fd, self._kill_r, self._kill_w = r, wfd, -1
            self._closed = False
            raise OSError(errno.ENOSPC, "inotify watch limit reached")

    with pytest.raises(OSError) as info:
        Inotify()
    fw._close_leaked_inotify(info.value)
    for fd in (r, wfd):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_config_first_run_only_when_file_missing(tmp_path, monkeypatch):
    from fast_file_search import config as config_mod

    monkeypatch.setattr(config_mod, "get_app_data_dir", lambda *_a, **_k: tmp_path)
    first = config_mod.ConfigManager()
    assert first.first_run is True
    first.config.index_paths = []
    first.save()

    again = config_mod.ConfigManager()
    assert again.first_run is False
    assert again.config.index_paths == []


def test_package_version_matches_pyproject():
    import re
    from pathlib import Path

    import fast_file_search

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == fast_file_search.__version__
