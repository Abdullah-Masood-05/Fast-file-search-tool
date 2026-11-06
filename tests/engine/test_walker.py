"""Walker rules (design C.1/C.3): symlinks, junctions, mount points, loops,
packages as leaves, dataless/hidden flag parsing (macOS st_flags and Windows
attributes, via fake stat objects), iCloud stubs, own files, drive-root names,
unreadable entries, and the walker pool's LIFO, backpressure and yielding."""
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from fast_file_search.engine.exclusions import Exclusions
from fast_file_search.engine.protocol import DATALESS, FOLDER, HIDDEN, MOUNT, PACKAGE, SYMLINK, SYSTEM
from fast_file_search.engine.walker import (DESCEND, FLAG_MASK, NOSTAT, SF_DATALESS, UF_HIDDEN, Activity, DirTask,
                                            WalkContext, WalkerPool, entry_for_path, list_dir, scan_entries,
                                            same_volume_devices)

IS_WINDOWS = sys.platform == "win32"


class FakeStat:
    def __init__(self, mode=stat.S_IFREG | 0o644, size=0, mtime=1000.0, flags=0, dev=1, ino=0, attrs=0, tag=0,
                 birth=500.0):
        self.st_mode, self.st_size, self.st_mtime = mode, size, mtime
        self.st_flags, self.st_dev, self.st_ino = flags, dev, ino
        self.st_file_attributes, self.st_reparse_tag, self.st_birthtime = attrs, tag, birth


class FakeEntry:
    def __init__(self, parent, name, st, sep="/"):
        self.name = name
        self.path = parent + name if parent.endswith(sep) else parent + sep + name
        self._st = st

    def stat(self, follow_symlinks=True):
        if isinstance(self._st, BaseException):
            raise self._st
        return self._st


DIR = stat.S_IFDIR | 0o755
LNK = stat.S_IFLNK | 0o777


def listing(ex, parent, items, sep="/", devices=None, root=""):
    ctx = WalkContext(ex, devices=devices or set(), root=root)
    return {t[0]: t for t in scan_entries(ctx, parent, [FakeEntry(parent, n, s, sep) for n, s in items])}


def test_real_listing_flags(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".hidden.txt").write_text("x")
    (tmp_path / "a.txt").write_text("hello")
    ctx = WalkContext(Exclusions(), root=str(tmp_path))
    lst = list_dir(DirTask(1, str(tmp_path), 0, False, (), None), ctx)
    got = {t[0]: t for t in lst.entries}
    assert set(got) == {"sub", ".hidden.txt", "a.txt"}
    assert got["sub"][4] & FOLDER and got["sub"][4] & DESCEND and got["sub"][5] == str(tmp_path / "sub")
    assert got["a.txt"][1] == 5 and got["a.txt"][5] is None and not got["a.txt"][4] & FOLDER
    assert got[".hidden.txt"][4] & HIDDEN
    assert got["a.txt"][2] == int(os.stat(tmp_path / "a.txt").st_mtime)
    if sys.platform in ("win32", "darwin"):
        assert got["a.txt"][3] > 0          # birth time
    missing = list_dir(DirTask(1, str(tmp_path / "nope"), 0, False, (), None), ctx)
    assert missing.entries is None and isinstance(missing.error, FileNotFoundError)


def test_symlinks_are_entries_never_followed(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f.txt").write_text("x")
    try:
        os.symlink(tmp_path / "d", tmp_path / "dlink", target_is_directory=True)
        os.symlink(tmp_path / "f.txt", tmp_path / "flink")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    got = {t[0]: t for t in list_dir(DirTask(1, str(tmp_path), 0, False, (), None),
                                      WalkContext(Exclusions(), root=str(tmp_path))).entries}
    for n in ("dlink", "flink"):
        assert got[n][4] & SYMLINK and not got[n][4] & (FOLDER | DESCEND) and got[n][5] is None


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows junctions")
def test_junctions_are_not_entered(tmp_path):
    (tmp_path / "real").mkdir()
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "junc"), str(tmp_path / "real")],
                          capture_output=True)
    if proc.returncode != 0:
        pytest.skip("cannot create a junction here")
    got = {t[0]: t for t in list_dir(DirTask(1, str(tmp_path), 0, False, (), None),
                                      WalkContext(Exclusions(), root=str(tmp_path))).entries}
    assert got["junc"][4] & FOLDER and got["junc"][4] & MOUNT and not got["junc"][4] & DESCEND
    assert got["real"][4] & DESCEND


def test_windows_mount_point_tag_and_attributes():
    ex = Exclusions(platform="win32")
    got = listing(ex, "C:\\r", [
        ("mnt", FakeStat(DIR, tag=0xA0000003, attrs=0x410)),
        ("cloud", FakeStat(DIR, tag=0x9000001A, attrs=0x10)),                 # OneDrive folder: descend
        ("online-only.docx", FakeStat(attrs=0x400000)),                        # RECALL_ON_DATA_ACCESS
        ("hid.txt", FakeStat(attrs=0x2)), ("sys.dll", FakeStat(attrs=0x4)),
    ], sep="\\")
    assert got["mnt"][4] & MOUNT and not got["mnt"][4] & DESCEND
    assert got["cloud"][4] & DESCEND
    assert got["online-only.docx"][4] & DATALESS
    assert got["hid.txt"][4] & HIDDEN and got["sys.dll"][4] & SYSTEM


def test_windows_drive_root_names_only_at_drive_root():
    ex = Exclusions(platform="win32", builtin=False)
    at_root = listing(ex, "C:\\", [("Windows", FakeStat(DIR)), ("$Recycle.Bin", FakeStat(DIR)),
                                   ("Users", FakeStat(DIR))], sep="\\")
    assert set(at_root) == {"Users"}
    deeper = listing(ex, "C:\\Users\\me", [("Windows", FakeStat(DIR))], sep="\\")
    assert "Windows" in deeper


def test_posix_mount_points_and_loops():
    ex = Exclusions(platform="linux")
    got = listing(ex, "/r", [("same", FakeStat(DIR, dev=1, ino=10)), ("other", FakeStat(DIR, dev=2, ino=11)),
                             ("loop", FakeStat(DIR, dev=1, ino=10))], devices={1})
    assert got["same"][4] & DESCEND
    assert got["other"][4] & MOUNT and not got["other"][4] & DESCEND
    assert not got["loop"][4] & DESCEND and not got["loop"][4] & MOUNT   # (dev, ino) already walked
    assert got["loop"][4] & FOLDER                                         # still indexed as an entry


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX device fencing on a real tree")
def test_real_tree_does_not_cross_devices(tmp_path):
    (tmp_path / "mnt").mkdir()
    ctx = WalkContext(Exclusions(), devices={-1}, root=str(tmp_path))     # everything is "another device"
    got = {t[0]: t for t in list_dir(DirTask(1, str(tmp_path), 0, False, (), None), ctx).entries}
    assert got["mnt"][4] & MOUNT and not got["mnt"][4] & DESCEND


def test_same_volume_devices():
    assert same_volume_devices("C:\\", "win32") == set()
    if not IS_WINDOWS:
        assert same_volume_devices("/") and same_volume_devices("/definitely/missing") == set()


def test_packages_are_leaves_on_macos():
    ex = Exclusions(platform="darwin")
    got = listing(ex, "/Applications", [("Safari.app", FakeStat(DIR)), ("Tools", FakeStat(DIR)),
                                        ("Doc.rtfd", FakeStat(DIR)), ("notes.app", FakeStat())])
    assert got["Safari.app"][4] & PACKAGE and got["Safari.app"][4] & FOLDER and not got["Safari.app"][4] & DESCEND
    assert got["Doc.rtfd"][4] & PACKAGE
    assert got["Tools"][4] & DESCEND and not got["Tools"][4] & PACKAGE
    assert not got["notes.app"][4] & PACKAGE              # a file, not a bundle
    inside = Exclusions(platform="darwin", descend_packages=True)
    got = listing(inside, "/Applications", [("Safari.app", FakeStat(DIR))])
    assert got["Safari.app"][4] & PACKAGE and got["Safari.app"][4] & DESCEND
    assert not listing(Exclusions(platform="linux"), "/a", [("x.app", FakeStat(DIR))])["x.app"][4] & PACKAGE


def test_macos_st_flags_dataless_and_hidden():
    ex = Exclusions(platform="darwin")
    got = listing(ex, "/Users/me/Docs", [
        ("cloud.pdf", FakeStat(flags=SF_DATALESS, size=12345)),
        ("secret", FakeStat(flags=UF_HIDDEN)),
        ("plain.txt", FakeStat()),
    ])
    assert got["cloud.pdf"][4] & DATALESS and got["cloud.pdf"][1] == 12345
    assert got["secret"][4] & HIDDEN and not got["secret"][4] & DATALESS
    assert got["plain.txt"][4] & FLAG_MASK == 0
    assert SF_DATALESS == 0x40000000 and UF_HIDDEN == 0x8000
    # st_flags are ignored where they do not exist (Linux)
    lin = listing(Exclusions(platform="linux"), "/x", [("cloud.pdf", FakeStat(flags=SF_DATALESS))])
    assert not lin["cloud.pdf"][4] & DATALESS


def test_icloud_stubs_use_the_real_name():
    ex = Exclusions(platform="darwin")
    got = listing(ex, "/Users/me/iCloud", [(".Report.pdf.icloud", FakeStat(size=180)),
                                           (".both.txt.icloud", FakeStat()), ("both.txt", FakeStat(size=9))])
    assert "Report.pdf" in got and got["Report.pdf"][4] & DATALESS and not got["Report.pdf"][4] & HIDDEN
    assert got["both.txt"][1] == 9 and not got["both.txt"][4] & DATALESS      # the real file wins
    assert ".Report.pdf.icloud" not in got


def test_index_hidden_off_drops_hidden_entries():
    ex = Exclusions(platform="win32", index_hidden=False, builtin=False)
    got = listing(ex, "C:\\r", [(".dot", FakeStat()), ("attr", FakeStat(attrs=0x2)), ("ok", FakeStat())], sep="\\")
    assert set(got) == {"ok"}


def test_own_files_are_dropped_near_own_dirs(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for n in ("index-v5.db", "index-v5.db-wal", "notes.txt"):
        (cache / n).write_text("x")
    (tmp_path / "other.txt").write_text("x")
    ex = Exclusions(own_dirs=[], own_files=[str(cache / "index-v5.db")])
    ctx = WalkContext(ex, root=str(cache))
    got = {t[0] for t in list_dir(DirTask(1, str(cache), 0, False, (), None), ctx).entries}
    assert got == {"notes.txt"}
    ex2 = Exclusions(own_dirs=[str(cache)])
    got = {t[0] for t in list_dir(DirTask(1, str(tmp_path), 0, False, (), None),
                                  WalkContext(ex2, root=str(tmp_path))).entries}
    assert got == {"other.txt"}                            # the cache dir itself is never indexed


def test_root_excludes_skipped_during_walk_only():
    ex = Exclusions(platform="linux")
    got = listing(ex, "/", [("proc", FakeStat(DIR)), ("tmp", FakeStat(DIR)), ("home", FakeStat(DIR))])
    assert set(got) == {"home"}
    got = listing(ex, "/home/me/project", [("tmp", FakeStat(DIR))])
    assert "tmp" in got


def test_unstatable_entries_are_kept_as_unknown():
    ex = Exclusions(platform="linux")
    got = listing(ex, "/r", [("gone", PermissionError(13, "denied")), ("ok", FakeStat())])
    assert got["gone"][4] & NOSTAT and not got["ok"][4] & NOSTAT


def test_user_patterns_and_sequences_in_walk():
    ex = Exclusions(["*.log", "build/output"], platform="linux")
    got = listing(ex, "/r/proj/build", [("output", FakeStat(DIR)), ("x.log", FakeStat()), ("keep", FakeStat())],
                  root="/r")
    assert set(got) == {"keep"}


def test_odd_names_survive(tmp_path):
    made = []
    candidates = ["lone\ud800surrogate.txt"] if IS_WINDOWS else [os.fsdecode(b"latin\xe9.txt"), "new\nline.txt",
                                                                   "back\\slash.txt"]
    for name in candidates:
        try:
            with open(os.path.join(str(tmp_path), name), "w") as fh:
                fh.write("x")
            if name in os.listdir(str(tmp_path)):
                made.append(name)
        except (OSError, UnicodeError, ValueError):
            continue
    if not made:
        pytest.skip("this file system rejects every odd name")
    got = {t[0] for t in list_dir(DirTask(1, str(tmp_path), 0, False, (), None),
                                  WalkContext(Exclusions(), root=str(tmp_path))).entries}
    assert set(made) <= got


def test_entry_for_path_matches_the_walk(tmp_path):
    (tmp_path / "a.txt").write_text("abc")
    (tmp_path / "node_modules").mkdir()
    ctx = WalkContext(Exclusions(), root=str(tmp_path))
    e = entry_for_path(ctx, str(tmp_path), "a.txt", str(tmp_path / "a.txt"))
    walked = {t[0]: t for t in list_dir(DirTask(1, str(tmp_path), 0, False, (), None), ctx).entries}
    assert e == walked["a.txt"]
    assert entry_for_path(ctx, str(tmp_path), "node_modules", str(tmp_path / "node_modules")) is None
    assert entry_for_path(ctx, str(tmp_path), "missing", str(tmp_path / "missing")) is None


class _Scan:
    def __init__(self, ctx, background=False):
        self.ctx, self.background, self.root_id = ctx, background, 1


def test_pool_lifo_backpressure_and_yield(tmp_path):
    for i in range(12):
        (tmp_path / f"d{i}").mkdir()
    ctx = WalkContext(Exclusions(), root=str(tmp_path))
    scan = _Scan(ctx)
    got = []
    gate = threading.Event()

    def deliver(lst):
        got.append(lst.task.path)
        gate.set()

    act = Activity()
    pool = WalkerPool(deliver, act, nthreads=1, max_inflight=3)
    try:
        tasks = [DirTask(i, str(tmp_path / f"d{i}"), 1, True, (), scan) for i in range(12)]
        pool.push(tasks)
        deadline = time.monotonic() + 5
        while len(got) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        assert len(got) == 3                         # backpressure: 3 in flight, nothing consumed
        assert got[0].endswith("d0")                 # first child first (LIFO with reversed push)
        act.poke(0.4)                                # a search: the walker must pause
        t0 = time.monotonic()
        pool.done()
        deadline = time.monotonic() + 5
        while len(got) < 4 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert time.monotonic() - t0 >= 0.3
        for _ in range(20):
            pool.done()
        deadline = time.monotonic() + 5
        while len(got) < 12 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(got) == 12
        assert pool.remove(lambda t: True) == []
    finally:
        pool.stop()
        pool.join()
