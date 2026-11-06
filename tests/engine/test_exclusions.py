"""Exclusion rules per OS (design C.3). Ported from tests/test_cross_platform.py:
per-OS system names, root excludes only at their absolute path, exclude-matcher
case rules (NFD on macOS), backslash rules, roots dedupe."""
import os
import unicodedata

import pytest

from fast_file_search.engine import exclusions as X
from fast_file_search.engine.exclusions import Exclusions

NFC_NAME = unicodedata.normalize("NFC", "Café Résumé.txt")
NFD_NAME = unicodedata.normalize("NFD", "Café Résumé.txt")


def test_system_folder_names_per_os():
    win, mac, linux = (X.system_folder_names(p) for p in ("win32", "darwin", "linux"))
    assert {"Windows", "Program Files", "$Recycle.Bin"} <= win
    assert "Windows" not in linux and "Program Files" not in mac
    assert {".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100", ".DS_Store", ".Trash",
            "Backups.backupdb"} <= mac
    assert "lost+found" in linux


def test_root_excludes_per_os():
    assert {"/proc", "/sys", "/dev", "/run", "/snap", "/tmp", "/var/cache", "/var/tmp"} == X.root_excludes("linux")
    assert {"/System", "/System/Volumes", "/Volumes", "/private/var", "/dev", "/.Spotlight-V100", "/.fseventsd",
            "/.vol", "/Network", "/cores"} == X.root_excludes("darwin")
    assert X.root_excludes("win32") == set()
    assert X.drive_root_names("win32") == {"Windows", "$Recycle.Bin", "System Volume Information"}


def test_root_excludes_only_apply_at_their_absolute_path():
    ex = Exclusions(platform="linux")
    assert ex.skip_dir("/tmp") and ex.skip_dir("/proc")
    assert not ex.skip_dir("/home/me/project/tmp")       # a project's tmp/ is data
    assert not ex.skip_name("tmp")
    assert not ex.skip_name("Windows")                   # user data on Linux
    win = Exclusions(platform="win32")
    assert win.skip_dir("C:\\Windows", parent_is_drive_root=True, name="Windows")
    assert win.skip_name("Windows")                      # v1 rule kept: system folder names anywhere on Windows


def test_builtin_names_everywhere():
    for p in ("win32", "darwin", "linux"):
        ex = Exclusions(platform=p)
        for n in ("node_modules", ".git", "__pycache__", ".venv", ".DS_Store", "Thumbs.db", ".pnpm-store", ".cache"):
            assert ex.skip_name(n), (p, n)
        assert not ex.skip_name(".github") and not ex.skip_name("my.git.notes") and not ex.skip_name("src")
    assert Exclusions(platform="linux").skip_name(".Trash-1000")
    assert not Exclusions(platform="win32").skip_name(".Trash-1000")
    assert not Exclusions(platform="linux", builtin=False).skip_name("node_modules")


def test_name_case_rules_per_os():
    lin = Exclusions(["node_modules", "*.TMP"], platform="linux", builtin=False)
    assert lin.skip_name("node_modules") and not lin.skip_name("Node_Modules")
    assert not lin.skip_name("x.tmp") and lin.skip_name("x.TMP")
    for p in ("win32", "darwin"):
        ex = Exclusions(["node_modules", "*.TMP", NFC_NAME], platform=p, builtin=False)
        assert ex.skip_name("Node_Modules") and ex.skip_name("x.tmp")
    mac = Exclusions([NFC_NAME], platform="darwin", builtin=False)
    assert mac.skip_name(NFD_NAME)                       # macOS hands out decomposed names
    assert not Exclusions([NFC_NAME], platform="linux", builtin=False).skip_name(NFD_NAME)


def test_globs_and_sequences():
    ex = Exclusions(["*.min.js", "build/output", "/dist/"], platform="linux", builtin=False)
    assert ex.skip_name("a.min.js") and not ex.skip_name("a.js")
    assert ex.skip_name("dist")                          # a single component with slashes = a name
    assert ex.seq_hit("output", "/r/proj/build", "/r")
    assert not ex.seq_hit("output", "/r/proj/other", "/r")
    assert not ex.seq_hit("output", "/r/build/x", "/r")
    w = Exclusions(["Build\\Output"], platform="win32", builtin=False)
    assert w.seq_hit("output", "C:\\r\\proj\\build", "C:\\r")


def test_path_rules():
    assert X.path_key("C:\\Users\\Me\\Docs\\", "win32") == X.path_key("c:/users/me/docs", "win32")
    assert X.is_path_under("c:\\USERS\\me\\docs\\a.txt", "C:\\Users\\Me", "win32")
    assert not X.is_path_under("C:\\Users\\Mex", "C:\\Users\\Me", "win32")
    assert X.is_path_under("D:\\x", "D:\\", "win32")
    assert X.path_key("/Users/me/" + NFD_NAME, "darwin") == X.path_key("/users/ME/" + NFC_NAME.upper(), "darwin")
    assert X.path_key("/home/A", "linux") != X.path_key("/home/a", "linux")
    assert not X.is_path_under("/a/b\\c", "/a/b", "linux")    # backslash is a name character on POSIX
    assert X.is_path_under("/anything", "/", "linux")


def test_dedupe_roots():
    assert X.dedupe_roots(["C:\\Data\\sub", "c:\\data", "C:\\DATA\\", "D:\\x", ""], "win32") == ["c:\\data", "D:\\x"]
    assert X.dedupe_roots(["/home/A", "/home/a", "/home/a/b"], "linux") == ["/home/A", "/home/a"]


def test_home_anchored_excludes():
    mac = Exclusions(platform="darwin", home="/Users/me")
    assert mac.skip_dir("/Users/me/Library") and mac.skip_dir("/users/me/.trash")
    assert not mac.skip_dir("/Users/me/Library/Mobile Documents")   # only the anchor itself is checked
    pre, exact = mac.below_root("/Users/me")
    assert "/users/me/library" in exact and "/users/me/library/" in pre
    pre, exact = mac.below_root("/Users/me/Library/Mobile Documents")  # a root inside it: nothing applies
    assert not exact
    lib = Exclusions(platform="darwin", home="/Users/me", include_home_library=True)
    assert not lib.skip_dir("/Users/me/Library")
    for sub in ("Caches", "Containers", "Group Containers", "Logs"):
        assert lib.skip_dir(f"/Users/me/Library/{sub}"), sub
    assert Exclusions(platform="win32", home="C:\\Users\\me").skip_dir("C:\\Users\\Me\\AppData")
    lin = Exclusions(platform="linux", home="/home/me")
    assert lin.skip_dir("/home/me/.cache") and lin.skip_dir("/home/me/.local/share/Trash")
    assert not lin.skip_dir("/home/me/.local/share")


def test_own_paths(tmp_path):
    cache, data = tmp_path / "cache", tmp_path / "data"
    ex = Exclusions(own_dirs=[str(cache), str(data)],
                    own_files=[str(cache / "index-v5.db"), str(data / "search_history.db")])
    for p in (cache, cache / "index-v5.db-wal", data / "search_history.db", data / "x" / "y"):
        assert ex.is_own(str(p)), p
    assert not ex.is_own(str(tmp_path / "cachex"))
    assert ex.near_own(str(tmp_path)) and ex.near_own(str(cache)) and not ex.near_own(str(tmp_path / "other"))
    assert ex.skip_dir(str(cache))
    # the own-file prefixes also catch them when a root is inside an own dir
    only_files = Exclusions(own_files=[str(cache / "index-v5.db")])
    assert only_files.is_own(str(cache / "index-v5.db-shm")) and not only_files.is_own(str(cache / "notes.txt"))


def test_packages_are_macos_only():
    assert Exclusions(platform="darwin").is_package("Safari.app")
    assert Exclusions(platform="darwin").is_package("Thing.FRAMEWORK")
    assert not Exclusions(platform="darwin").is_package("app")
    assert not Exclusions(platform="win32").is_package("Safari.app")


@pytest.mark.skipif(os.name != "nt", reason="Windows separators")
def test_backslash_is_a_separator_on_windows():
    ex = Exclusions(["build\\out"], builtin=False)
    assert ex.seq_hit("out", "C:\\repo\\build", "C:\\repo")
