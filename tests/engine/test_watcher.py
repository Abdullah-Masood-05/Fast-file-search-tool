"""Watcher (design D.1): coalescer debounce and max latency, flood collapse,
own-file and exclude drops, the move pairing table, dir-modified noise,
synthetic events. Ported from tests/test_cross_platform.py: event paths map to
the root spelling (macOS /private/var alias, case), excludes judged below the
root with case rules, set_paths dedupe, inotify-limit degrade and the leaked fd."""
import errno
import logging
import os
import sys
import threading
import time

import pytest

from fast_file_search.engine import watcher as W
from fast_file_search.engine.exclusions import Exclusions
from fast_file_search.engine.watcher import DIR, FILE, RECURSIVE, Coalescer, Watcher


class Sink:
    def __init__(self):
        self.batches = []
        self.times = []
        self.ev = threading.Event()

    def __call__(self, b):
        self.batches.append(b)
        self.times.append(time.monotonic())
        self.ev.set()


def test_coalescer_flushes_after_quiet_period():
    sink = Sink()
    c = Coalescer(sink, quiet=0.3, max_wait=2.0)
    try:
        t0 = time.monotonic()
        c.add("/r/a", FILE)
        c.add("/r/a", FILE)
        c.add("/r/b", DIR)
        assert sink.ev.wait(2)
        dt = sink.times[0] - t0
        assert 0.25 <= dt < 1.0
        assert sink.batches[0].paths == {"/r/a": FILE, "/r/b": DIR}
        assert len(sink.batches) == 1
    finally:
        c.stop()


def test_coalescer_max_latency_under_a_continuous_stream():
    sink = Sink()
    c = Coalescer(sink, quiet=0.5, max_wait=1.0)
    try:
        t0 = time.monotonic()
        while not sink.ev.is_set() and time.monotonic() - t0 < 3:
            c.add(f"/r/f{int((time.monotonic() - t0) * 100)}", FILE)
            time.sleep(0.05)                     # never 0.5 s quiet
        assert sink.ev.is_set()
        assert 0.9 <= sink.times[0] - t0 < 1.6
    finally:
        c.stop()


def test_flood_collapses_to_common_ancestors(tmp_path):
    sink = Sink()
    ex = Exclusions(platform=sys.platform)
    w = Watcher(ex, sink, quiet=0.2, max_wait=1.0, max_pending=100)
    root = str(tmp_path)
    w.set_roots([root])
    try:
        for d in range(40):
            for i in range(5):
                assert w.feed("created", os.path.join(root, "proj", f"d{d}", f"f{i}.txt"))
        assert sink.ev.wait(3)
        b = sink.batches[0]
        assert b.collapsed and all(bits == RECURSIVE for bits in b.paths.values())
        assert 1 <= len(b.paths) <= 16
        assert all(p.startswith(root) for p in b.paths)
        # 40 dirs under proj/ collapse to proj (or the root)
        assert set(b.paths) <= {os.path.join(root, "proj"), root}
    finally:
        w.stop()


def test_overflow_requests_a_root_rescan(tmp_path):
    sink = Sink()
    w = Watcher(Exclusions(), sink, quiet=0.1, max_wait=0.5)
    w.set_roots([str(tmp_path)])
    try:
        w.coalescer.overflow(str(tmp_path))
        assert sink.ev.wait(2)
        assert sink.batches[0].collapsed and sink.batches[0].paths == {str(tmp_path): RECURSIVE}
    finally:
        w.stop()


def _watcher(root, platform=None, **kw):
    ex = Exclusions(kw.pop("patterns", ()), platform=platform, **kw)
    w = Watcher(ex, lambda b: None, quiet=60, max_wait=60)
    w.set_roots([root], check_exists=False)
    return w


def test_own_files_and_excluded_components_are_dropped(tmp_path):
    cache = tmp_path / "cache"
    ex = Exclusions(["*.tmp"], own_dirs=[str(cache)], own_files=[str(cache / "index-v5.db")])
    w = Watcher(ex, lambda b: None, quiet=60, max_wait=60)
    w.set_roots([str(tmp_path)], check_exists=False)
    try:
        sep = os.sep
        drop = [f"{cache}{sep}index-v5.db-wal", f"{cache}{sep}index-v5.snap", f"{tmp_path}{sep}proj{sep}node_modules"
                f"{sep}x{sep}a.js", f"{tmp_path}{sep}.git{sep}HEAD", f"{tmp_path}{sep}a.tmp",
                f"{tmp_path}{sep}outside{sep}..{sep}..{sep}elsewhere"[:0] + "Z:\\nowhere\\x"]
        for p in drop:
            assert not w.feed("modified", p), p
        assert w.feed("modified", f"{tmp_path}{sep}proj{sep}src{sep}a.js")
        assert w.feed("created", f"{tmp_path}{sep}.github{sep}ci.yml")        # not .git
        assert w.coalescer.pending == 2
    finally:
        w.stop()


def test_history_and_index_db_writes_queue_nothing(tmp_path):
    """Real observer: the app's own DB/WAL/history writes inside a watched root
    produce 0 queued paths."""
    root = tmp_path / "root"
    cache = root / "cache"
    data = root / "data"
    cache.mkdir(parents=True)
    data.mkdir()
    sink = Sink()
    ex = Exclusions(own_dirs=[str(cache), str(data)],
                    own_files=[str(cache / "index-v5.db"), str(data / "search_history.db")])
    w = Watcher(ex, sink, quiet=0.2, max_wait=1.0)
    w.set_roots([str(root)])
    assert w.start() == {str(root): "on"}
    try:
        time.sleep(0.2)
        for i in range(30):
            for p in (cache / "index-v5.db", cache / "index-v5.db-wal", data / "search_history.db",
                      data / "search_history.db-journal"):
                with open(p, "ab") as fh:
                    fh.write(b"x" * 100)
        time.sleep(1.0)
        assert w.events_seen > 0                 # events did arrive ...
        assert w.events_kept == 0 and w.coalescer.pending == 0 and not sink.batches   # ... and were all dropped
        (root / "real.txt").write_text("x")
        assert sink.ev.wait(3)
        assert any(p.endswith("real.txt") for b in sink.batches for p in b.paths)
    finally:
        w.stop()


def test_move_pairing_table(tmp_path):
    root = str(tmp_path)
    w = _watcher(root)
    sep = os.sep
    a, b = f"{root}{sep}a.txt", f"{root}{sep}sub{sep}b.txt"
    nm = f"{root}{sep}node_modules{sep}c.txt"
    out = "Z:\\outside\\d.txt" if os.name == "nt" else "/outside/d.txt"
    try:
        assert w.feed("moved", a, False, b)                      # in -> in: a move
        assert w.coalescer._moves == [(a, b, False)]
        assert w.feed("moved", nm, False, a)                     # excluded -> in: appears
        assert w.feed("moved", out, False, f"{root}{sep}new.txt")  # outside -> in: appears
        assert w.feed("moved", f"{root}{sep}gone.txt", False, nm)  # in -> excluded: disappears
        assert w.feed("moved", f"{root}{sep}gone2.txt", False, out)  # in -> outside: disappears
        assert not w.feed("moved", nm, False, out)               # neither end indexed
        assert w.coalescer._paths == {a: FILE, f"{root}{sep}new.txt": FILE, f"{root}{sep}gone.txt": FILE,
                                      f"{root}{sep}gone2.txt": FILE}
        assert not w.feed("moved", a, True, b, synthetic=True)   # watchdog's sub-move echoes
        assert not w.feed("created", a, False, synthetic=True)
    finally:
        w.stop()


@pytest.mark.parametrize("platform,kept", [("win32", False), ("linux", False), ("darwin", True)])
def test_dir_modified_noise(platform, kept):
    root = "C:\\r" if platform == "win32" else "/r"
    sep = "\\" if platform == "win32" else "/"
    w = _watcher(root, platform)
    try:
        assert w.feed("modified", root + sep + "sub", True) is kept
        if kept:
            assert w.coalescer._paths == {root + sep + "sub": DIR}
        assert w.feed("modified", root + sep + "f.txt", False)
    finally:
        w.stop()


def test_watcher_maps_event_paths_to_root_spelling():
    w = _watcher("/var/folders/xy/root", "darwin")
    w.add_alias("/private/var/folders/xy/root", "/var/folders/xy/root")
    try:
        assert w.map_path("/private/var/folders/xy/root/a/b.txt")[0] == "/var/folders/xy/root/a/b.txt"
        # different case of the configured root on a case-insensitive volume
        assert w.map_path("/VAR/folders/XY/root/c.txt")[0] == "/var/folders/xy/root/c.txt"
        assert w.map_path("/elsewhere/x") is None
        assert w.feed("created", "/private/var/folders/xy/root/a/b.txt")
        assert "/var/folders/xy/root/a/b.txt" in w.coalescer._paths
    finally:
        w.stop()


def test_watcher_judges_excludes_below_root_with_case_rules():
    w = _watcher("/Users/me/Projects/app", "darwin", patterns=["projects"])
    try:
        assert w.accept("/users/me/projects/app/main.py") is not None    # the root's own components don't count
        assert w.accept("/Users/me/Projects/app/projects/x.py") is None
        assert w.accept("/Users/me/Projects/app/Node_Modules/x.js") is None
    finally:
        w.stop()
    lin = _watcher("/home/me/app", "linux")
    try:
        assert lin.accept("/home/me/app/Node_Modules/x.js") is not None    # case-sensitive on Linux
        assert lin.accept("/home/me/app/node_modules/x.js") is None
    finally:
        lin.stop()


def test_home_anchored_excludes_in_events():
    w = _watcher("/Users/me", "darwin", home="/Users/me")
    try:
        assert w.accept("/Users/me/Library/Caches/x") is None
        assert w.accept("/Users/me/Library") is None
        assert w.accept("/Users/me/Library2/x") is not None
        assert w.accept("/Users/me/Documents/x") is not None
    finally:
        w.stop()
    inside = _watcher("/Users/me/Library/Mobile Documents", "darwin", home="/Users/me")
    try:
        assert inside.accept("/Users/me/Library/Mobile Documents/a.txt") is not None
    finally:
        inside.stop()


def test_packages_and_hidden_in_events():
    w = _watcher("/Applications", "darwin")
    try:
        assert w.accept("/Applications/Safari.app") is not None           # the package itself is an entry
        assert w.accept("/Applications/Safari.app/Contents/Info.plist") is None
    finally:
        w.stop()
    h = _watcher("/r", "linux", index_hidden=False)
    try:
        assert h.accept("/r/.config/x") is None and h.accept("/r/a/b") is not None
    finally:
        h.stop()


def test_watcher_set_paths_dedupes(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    w = Watcher(Exclusions(), lambda b: None)
    try:
        assert w.set_roots([str(tmp_path / "a" / "b"), str(tmp_path / "a"), str(tmp_path / "missing")]) == \
            [str(tmp_path / "a")]
        assert w.roots == [str(tmp_path / "a")]
    finally:
        w.stop()


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
def test_watcher_degrades_on_inotify_limits(tmp_path, caplog, fail_all):
    roots = [tmp_path / "r1", tmp_path / "r2", tmp_path / "r3"]
    for r in roots:
        r.mkdir()
    _FakeObserver.instances = []
    _FakeObserver.fail_for = {str(r) for r in (roots if fail_all else roots[:2])}
    errors = []
    w = Watcher(Exclusions(), lambda b: None, observer_factory=_FakeObserver,
                on_error=lambda root, exc: errors.append(exc))
    w.set_roots([str(r) for r in roots])
    with caplog.at_level(logging.WARNING, logger=W.__name__):
        live = w.start()
    obs = _FakeObserver.instances[-1]
    try:
        assert len(errors) == 1 and errors[0].errno == errno.ENOSPC      # reported once
        assert sum("Live file watching limited" in r.message for r in caplog.records) == 1
        if fail_all:
            assert not w.is_running and obs.stopped
            assert set(live.values()) == {"limited"}
        else:
            assert w.is_running and obs.scheduled == [str(roots[2])]
            assert live[str(roots[2])] == "on" and live[str(roots[0])] == "limited"
    finally:
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
    W.close_leaked_inotify(info.value)
    for fd in (r, wfd):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_real_observer_reports_changes_in_root_spelling(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    sink = Sink()
    w = Watcher(Exclusions(), sink, quiet=0.2, max_wait=1.0)
    w.set_roots([str(root)])
    assert w.start()[str(root)] == "on"
    try:
        time.sleep(0.2)
        (root / "sub" / "new.txt").write_text("x")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "skip.js").write_text("x")
        os.rename(root / "sub", root / "sub2")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not any(b.moves for b in sink.batches):
            time.sleep(0.05)
        paths = {p for b in sink.batches for p in b.paths}
        moves = [m for b in sink.batches for m in b.moves]
        assert (str(root / "sub"), str(root / "sub2"), True) in moves
        assert not any("node_modules" in p for p in paths)
    finally:
        w.stop()


def test_feed_cost_per_event():
    """The filter itself is cheap: 20k mixed events well under 0.2 s (p8 measured ~1 us/event)."""
    root = "C:\\Users\\A\\Documents" if os.name == "nt" else "/home/a/Documents"
    sep = os.sep
    cache = ("C:\\Users\\A\\AppData\\Local\\FFS" if os.name == "nt" else "/home/a/.cache/ffs")
    ex = Exclusions(own_dirs=[cache])
    w = Watcher(ex, lambda b: None, quiet=60, max_wait=60, max_pending=10 ** 6)
    w.set_roots([root], check_exists=False)
    try:
        evs = []
        for i in range(20000):
            if i % 10 < 5:
                evs.append(f"{root}{sep}proj{sep}node_modules{sep}pkg{i % 300}{sep}lib{sep}f{i}.js")
            elif i % 10 == 5:
                evs.append(f"{cache}{sep}index-v5.db-wal")
            else:
                evs.append(f"{root}{sep}proj{sep}src{sep}comp{i % 50}{sep}File{i}.tsx")
        t0 = time.perf_counter()
        kept = sum(w.feed("modified", p) for p in evs)
        dt = time.perf_counter() - t0
        assert kept == 8000
        assert dt < 0.2, f"{dt * 1e6 / len(evs):.2f} us/event"
    finally:
        w.stop()
