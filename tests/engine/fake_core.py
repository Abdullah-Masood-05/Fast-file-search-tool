"""A dict-backed stand-in for P1's core (engine/ops, store, memindex, userdata).

It implements the frozen H.1 signatures closely enough for P2's service tests:
op semantics (INS/UPD/REN/DEL/DIR/DIRPATH/DIRMTIME/ROOTDEL), generations,
children()/dir_lookup()/entry_lookup(), a naive substring search with exact
counts and a few filters, counters(), snapshots (pickle) and user data.

Used when ``FFS_ENGINE_CORE=fake_core`` is set (see engine.service._load_core).
"""
from __future__ import annotations

import os
import pickle
import threading
import time
from pathlib import Path
from typing import NamedTuple

from fast_file_search.engine.protocol import (
    FOLDER, ResultRow, SearchResult, Suggestion,
)

INS, UPD, REN, DEL, DIR, DIRPATH, DIRMTIME, ROOTDEL = 1, 2, 3, 4, 5, 6, 7, 8

_SEP = "\\" if os.name == "nt" else "/"


class StoreCorrupt(Exception):
    pass


class RootRow(NamedTuple):
    id: int
    path: str
    state: str
    entries: int
    folders: int
    bytes: int
    denied_dirs: int
    last_scan_end: int | None
    last_scan_complete: int


class RootCounts(NamedTuple):
    entries: int
    folders: int
    bytes: int
    denied_dirs: int


class IndexCounters(NamedTuple):
    total_entries: int
    total_folders: int
    total_bytes: int
    roots: dict            # root_id -> RootCounts
    top_exts: tuple = ()


class HistoryItem(NamedTuple):
    query: str
    count: int
    pinned: bool
    last_used: float


class _Data:
    def __init__(self):
        self.gen = 0
        self.next_id = 1
        self.roots = {}       # id -> dict(path, state, ...)
        self.dirs = {}        # id -> [parent_id, root_id, path, mtime, flags]
        self.entries = {}     # id -> [dir_id, name, size, mtime, ctime, flags]
        self.by_dir = {}      # dir_id -> {name: id}
        self.clean = 1


def _apply(d: _Data, ops):
    for op in ops:
        t = op[0]
        if t == INS:
            _, i, did, name, size, mtime, ctime, flags = op
            if name in d.by_dir.setdefault(did, {}):
                raise ValueError(f"UNIQUE constraint failed: entries.dir_id, entries.name ({did}, {name!r})")
            d.entries[i] = [did, name, size, mtime, ctime, flags]
            d.by_dir[did][name] = i
        elif t == UPD:
            _, i, size, mtime, ctime, flags = op
            e = d.entries[i]
            e[2:6] = [size, mtime, ctime, flags]
        elif t == REN:
            _, i, ndid, nname = op
            e = d.entries[i]
            del d.by_dir[e[0]][e[1]]
            if nname in d.by_dir.setdefault(ndid, {}):
                raise ValueError("UNIQUE constraint failed (REN)")
            e[0], e[1] = ndid, nname
            d.by_dir[ndid][nname] = i
            if i in d.dirs:
                d.dirs[i][0] = ndid
        elif t == DEL:
            _del_entry(d, op[1])
        elif t == DIR:
            _, did, parent, root, path, mtime = op
            d.dirs[did] = [parent, root, path, mtime, 0]
            d.by_dir.setdefault(did, {})
        elif t == DIRPATH:
            _, did, new = op
            old = d.dirs[did][2]
            for k, v in d.dirs.items():
                p = v[2]
                if p == old:
                    v[2] = new
                elif p.startswith(old + _SEP) or (old.endswith(_SEP) and p.startswith(old)):
                    v[2] = new + p[len(old):]
        elif t == DIRMTIME:
            _, did, mtime, flags = op
            if did in d.dirs:
                d.dirs[did][3] = mtime
                d.dirs[did][4] = flags
        elif t == ROOTDEL:
            rid = op[1]
            for i in list(d.by_dir.get(rid, {}).values()):
                _del_entry(d, i)
            d.dirs.pop(rid, None)
            d.by_dir.pop(rid, None)
            d.roots.pop(rid, None)
        else:
            raise ValueError(f"bad op {op!r}")


def _del_entry(d: _Data, i):
    e = d.entries.pop(i, None)
    if e is None:
        return
    d.by_dir.get(e[0], {}).pop(e[1], None)
    if i in d.dirs:
        for c in list(d.by_dir.get(i, {}).values()):
            _del_entry(d, c)
        d.dirs.pop(i, None)
        d.by_dir.pop(i, None)


class LoadChunk(NamedTuple):
    entries: list
    dirs: list
    roots: list


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pending_root = {}
        if self.path.exists() and self.path.stat().st_size:
            try:
                with open(self.path, "rb") as fh:
                    self.d = pickle.load(fh)
            except Exception as exc:
                raise StoreCorrupt(str(exc)) from exc
        else:
            self.d = _Data()
            self._save()

    @staticmethod
    def open_or_recover(path: Path):
        try:
            return Store(path), False
        except StoreCorrupt:
            p = Path(path)
            os.replace(p, p.with_name(p.name + f".corrupt-{int(time.time())}"))
            return Store(path), True

    def _save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(self.d, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, self.path)

    @property
    def generation(self) -> int:
        return self.d.gen

    def allocate_ids(self, n: int) -> range:
        r = range(self.d.next_id, self.d.next_id + n)
        self.d.next_id += n
        return r

    def roots(self):
        out = []
        for rid, r in self.d.roots.items():
            out.append(RootRow(rid, r["path"], r.get("state", "new"), r.get("entries", 0), r.get("folders", 0),
                               r.get("bytes", 0), r.get("denied_dirs", 0), r.get("last_scan_end"),
                               r.get("last_scan_complete", 0)))
        return out

    def add_root(self, path: str) -> int:
        rid = self.allocate_ids(1)[0]
        self.d.roots[rid] = {"path": path, "state": "new", "added_at": int(time.time())}
        self.d.dirs[rid] = [0, rid, path, 0, 0]
        self.d.by_dir[rid] = {}
        self.d.gen += 1
        self._save()
        return rid

    def set_root_state(self, root_id: int, **fields) -> None:
        self._pending_root.setdefault(root_id, {}).update(fields)

    def dir_lookup(self, path: str):
        for did, v in self.d.dirs.items():
            if v[2] == path:
                return did, v[1]
        return None

    def children(self, dir_id: int):
        out = []
        for name, i in self.d.by_dir.get(dir_id, {}).items():
            e = self.d.entries[i]
            out.append((i, e[1], e[2], e[3], e[4], e[5]))
        return out

    def entry_lookup(self, dir_id: int, name: str):
        i = self.d.by_dir.get(dir_id, {}).get(name)
        if i is None:
            return None
        e = self.d.entries[i]
        return (i, e[2], e[3], e[4], e[5])

    def apply(self, ops) -> int:
        _apply(self.d, ops)
        for rid, f in self._pending_root.items():
            if rid in self.d.roots:
                self.d.roots[rid].update(f)
        self._pending_root.clear()
        self.d.gen += 1
        self._save()
        return self.d.gen

    def iter_load(self):
        yield LoadChunk(list(self.d.entries.items()), list(self.d.dirs.items()), self.roots())

    def mark_open(self) -> bool:
        prev = bool(self.d.clean)
        self.d.clean = 0
        self._save()
        return prev

    def close(self, clean: bool = True) -> None:
        self.d.clean = 1 if clean else 0
        self._save()


def _fold(s: str) -> str:
    return s.casefold()


class MemIndex:
    def __init__(self):
        self.d = _Data()
        self.generation = 0
        self._lock = threading.Lock()
        self._last = {}

    @classmethod
    def build(cls, store: Store) -> "MemIndex":
        m = cls()
        m.d = pickle.loads(pickle.dumps(store.d))
        m.generation = store.generation
        return m

    @classmethod
    def load_snapshot(cls, path: Path, expect_generation: int):
        try:
            with open(path, "rb") as fh:
                d = pickle.load(fh)
        except Exception:
            return None
        if d.gen != expect_generation:
            return None
        m = cls()
        m.d = d
        m.generation = d.gen
        return m

    def save_snapshot(self, path: Path) -> None:
        with self._lock:
            self.d.gen = self.generation
            data = pickle.dumps(self.d)
        tmp = Path(str(path) + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def apply(self, ops, generation: int) -> None:
        with self._lock:
            _apply(self.d, ops)
            self.generation = generation

    # ------------------------------------------------------------------ queries
    def path_of(self, entry_id: int):
        e = self.d.entries.get(entry_id)
        if e is None:
            return None
        base = self.d.dirs[e[0]][2]
        return base + e[1] if base.endswith(_SEP) else base + _SEP + e[1]

    def _root_of_dir(self, did):
        return self.d.dirs[did][1]

    def _matches(self, e, terms, f):
        name = _fold(e[1])
        if any(t not in name for t in terms):
            return False
        flags = e[5]
        if f.kinds:
            k = "folder" if flags & FOLDER else "file"
            if k not in f.kinds and not ("file" in f.kinds and not flags & FOLDER):
                return False
        if f.exts:
            ext = name.rsplit(".", 1)[-1] if "." in name else ""
            if flags & FOLDER or ext not in f.exts:
                return False
        if f.size_min is not None and (flags & FOLDER or e[2] < f.size_min):
            return False
        if f.size_max is not None and (flags & FOLDER or e[2] >= f.size_max):
            return False
        return True

    def search(self, req, cancel, user):
        t0 = time.perf_counter()
        terms = [_fold(t) for t in req.text.split() if t]
        with self._lock:
            hits = []
            for i, e in self.d.entries.items():
                if e[0] not in self.d.dirs:
                    continue
                if self._matches(e, terms, req.filters):
                    hits.append(i)
            if cancel():
                return None
            rc = {}
            for i in hits:
                r = self._root_of_dir(self.d.entries[i][0])
                rc[r] = rc.get(r, 0) + 1
            if req.filters.root_ids:
                hits = [i for i in hits if self._root_of_dir(self.d.entries[i][0]) in req.filters.root_ids]
            hits.sort(key=lambda i: (_fold(self.d.entries[i][1]), i))
            self._last = {"gen": req.gen, "hits": hits, "req": req}
            rows = tuple(self._row(i, user) for i in hits[: req.limit])
            gen = self.generation
        return SearchResult(gen=req.gen, total=len(hits), offset=0, rows=rows,
                            elapsed_ms=(time.perf_counter() - t0) * 1000,
                            root_counts=tuple(sorted(rc.items())), suggestions=(),
                            index_generation=gen, terms=tuple(terms))

    def _row(self, i, user):
        e = self.d.entries[i]
        did = e[0]
        rid = self._root_of_dir(did)
        root_path = self.d.dirs[rid][2]
        dpath = self.d.dirs[did][2]
        rel = dpath[len(root_path):].lstrip("\\/")
        path = self.path_of(i)
        fav = False
        try:
            fav = path in user.favorite_paths()
        except Exception:
            pass
        return ResultRow(i, e[1], path, rid, rel, e[2], e[3], e[5], (), fav)

    def page(self, gen: int, offset: int, limit: int):
        with self._lock:
            if self._last.get("gen") != gen:
                return None
            hits = self._last["hits"]
            rows = tuple(self._row(i, None) for i in hits[offset: offset + limit] if i in self.d.entries)
            return SearchResult(gen=gen, total=len(hits), offset=offset, rows=rows, elapsed_ms=0.0,
                                index_generation=self.generation)

    def count(self, text, filters, stop_at=None) -> int:
        terms = [_fold(t) for t in text.split() if t]
        with self._lock:
            return sum(1 for e in self.d.entries.values() if self._matches(e, terms, filters))

    def counters(self) -> IndexCounters:
        with self._lock:
            per = {}
            te = tf = tb = 0
            for e in self.d.entries.values():
                d = self.d.dirs.get(e[0])
                if d is None:
                    continue
                rid = d[1]
                c = per.setdefault(rid, [0, 0, 0, 0])
                c[0] += 1
                te += 1
                if e[5] & FOLDER:
                    c[1] += 1
                    tf += 1
                else:
                    c[2] += e[2]
                    tb += e[2]
            for rid in self.d.roots or ():
                per.setdefault(rid, [0, 0, 0, 0])
            for did, v in self.d.dirs.items():
                if v[0] == 0:
                    per.setdefault(did, [0, 0, 0, 0])
            return IndexCounters(te, tf, tb, {k: RootCounts(*v) for k, v in per.items()})

    def compact_if_needed(self) -> bool:
        return False

    def ids_for_paths(self, paths):
        want = set(paths)
        return [i for i in self.d.entries if self.path_of(i) in want]


class UserData:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hist = {}
        self._pins = set()
        self._fav = set()
        self._tags = {}
        self._notes = {}
        self.favorites_generation = 0
        self.closed = False

    def record_search(self, text: str, result_count: int) -> None:
        self._hist[text] = (result_count, time.time())

    def history(self, prefix: str = "", limit: int = 20):
        items = [HistoryItem(q, c, q in self._pins, t) for q, (c, t) in self._hist.items() if q.startswith(prefix)]
        items.sort(key=lambda h: (not h.pinned, -h.last_used))
        return items[:limit]

    def pin(self, text):
        self._pins.add(text)

    def unpin(self, text):
        self._pins.discard(text)

    def clear_history(self):
        self._hist.clear()

    def set_favorite(self, path: str, on: bool):
        (self._fav.add if on else self._fav.discard)(path)
        self.favorites_generation += 1

    def favorite_paths(self):
        return frozenset(self._fav)

    def tags(self, path):
        return list(self._tags.get(path, []))

    def set_tags(self, path, tags):
        self._tags[path] = list(tags)

    def note(self, path):
        return self._notes.get(path, "")

    def set_note(self, path, note):
        self._notes[path] = note

    def forget(self, paths):
        for p in paths:
            self._hist.pop(p, None)

    def backup_if_due(self):
        pass

    def close(self):
        self.closed = True


# =============================================================================== test harness
# Shared by P2's tests (tests/engine/test_*.py). Not part of the fake core API.

import importlib
import importlib.util
import sys as _sys


def real_core_available() -> bool:
    try:
        for m in ("store", "memindex", "userdata", "ops"):
            if importlib.util.find_spec(f"fast_file_search.engine.{m}") is None:
                return False
            importlib.import_module(f"fast_file_search.engine.{m}")
        return True
    except Exception:
        return False


def select_core(monkeypatch) -> str:
    """Use P1's real core when it imports (``FFS_TEST_CORE=fake`` forces the fake)."""
    want = os.environ.get("FFS_TEST_CORE", "auto")
    if want != "fake" and (want == "real" or real_core_available()):
        monkeypatch.delenv("FFS_ENGINE_CORE", raising=False)
        return "real"
    monkeypatch.setenv("FFS_ENGINE_CORE", "fake_core")
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in _sys.path:
        _sys.path.insert(0, here)
    return "fake"


def make_boot(tmp, roots, **cfg):
    from fast_file_search.engine import protocol as P
    tmp = Path(tmp)
    for d in ("cache", "data", "logs"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    return P.EngineBootstrap(P.EngineConfig(roots=tuple(str(r) for r in roots), **cfg),
                             str(tmp / "cache"), str(tmp / "data"), str(tmp / "logs"))


class Harness:
    """An in-process Engine with a message collector."""

    def __init__(self, boot):
        from fast_file_search.engine.service import Engine
        self.msgs = []
        self.cv = threading.Condition()
        self.boot = boot
        self.engine = Engine(boot, self._send)
        self.gen = 0

    def _send(self, m):
        with self.cv:
            self.msgs.append(m)
            self.cv.notify_all()

    def start(self, timeout=20):
        from fast_file_search.engine import protocol as P
        self.engine.start()
        return self.wait(lambda m: isinstance(m, (P.Ready, P.Fatal)), timeout)

    def wait(self, pred, timeout=10.0, start=0):
        end = time.monotonic() + timeout
        with self.cv:
            while True:
                for m in self.msgs[start:]:
                    if pred(m):
                        return m
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timeout; got {[type(m).__name__ for m in self.msgs[-20:]]}")
                self.cv.wait(min(left, 0.1))

    def of(self, cls):
        with self.cv:
            return [m for m in self.msgs if isinstance(m, cls)]

    def search(self, text, filters=None, timeout=10.0, **kw):
        from fast_file_search.engine import protocol as P
        self.gen += 1
        g = self.gen
        mark = len(self.msgs)
        self.engine.handle(P.SearchRequest(g, text, filters or P.FilterState(), **kw))
        return self.wait(lambda m: isinstance(m, P.SearchResult) and m.gen == g and not m.refresh, timeout, mark)

    def call(self, fn, timeout=30.0):
        """Run ``fn()`` on the writer thread (the only Store user) and return its result."""
        done = threading.Event()
        box = {}

        def run():
            try:
                box["v"] = fn()
            except BaseException as exc:  # surfaced below
                box["e"] = exc
            done.set()
        self.engine._wq.put(("call", run))
        assert done.wait(timeout), "writer did not run the call"
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def settle(self, timeout=20.0, quiet=0.3):
        """Wait until no scan, listing, watcher batch or op is pending."""
        e = self.engine
        end = time.monotonic() + timeout
        stable_since = None
        while time.monotonic() < end:
            w = e.watcher
            busy = (e._scans_active() or not e._wq.empty() or bool(e._ops)
                    or (w is not None and w.coalescer.pending > 0))
            if not busy:
                busy = self.call(lambda: e._scans_active() or bool(e._ops))
            now = time.monotonic()
            if busy:
                stable_since = None
            elif stable_since is None:
                stable_since = now
            elif now - stable_since >= quiet:
                return
            time.sleep(0.05)
        raise AssertionError("engine did not settle")

    def _walk_store(self, with_mem: bool):
        from fast_file_search.engine import protocol as P
        e = self.engine

        def collect():
            st = e.store
            out = {}
            for r in st.roots():
                stack = [(r.id, r.path)]
                while stack:
                    did, base = stack.pop()
                    for (i, name, size, mtime, ctime, flags) in st.children(did):
                        p = base + name if base.endswith(os.sep) else base + os.sep + name
                        out[i] = (p, flags, size, mtime)
                        if flags & FOLDER:
                            stack.append((i, p))
            if not with_mem:
                return out, None, None
            mem_paths = {i: e.mem.path_of(i) for i in out}
            return out, mem_paths, e.mem.counters().total_entries
        return self.call(collect)

    def index_paths(self):
        """Every indexed path, read from the Store, checked against the MemIndex."""
        out, mem_paths, mem_total = self._walk_store(True)
        for i, (p, *_r) in out.items():
            assert mem_paths[i] == p, (i, p, mem_paths[i])
        assert mem_total == len(out), (mem_total, len(out))
        return {v[0] for v in out.values()}

    def entries(self):
        """path -> (flags, size, mtime) from the Store."""
        out, _m, _t = self._walk_store(False)
        return {p: (f, s, m) for p, f, s, m in out.values()}

    def record_ops(self):
        """Wrap Store.apply to record every committed op list (writer thread)."""
        e = self.engine
        log = []

        def install():
            st = e.store
            orig = st.apply

            def apply(ops):
                log.append(list(ops))
                return orig(ops)
            st.apply = apply
        self.call(install)
        return log

    def stop(self):
        self.engine.stop(5.0)


def walk_truth(root, skip_names=None):
    """Ground truth: every path below ``root`` (os.walk, no symlink following),
    minus entries whose name is in ``skip_names`` (and their subtrees)."""
    from fast_file_search.engine.exclusions import COMMON_NAMES, system_folder_names
    skip = set(skip_names) if skip_names is not None else set(COMMON_NAMES) | system_folder_names()
    fold = os.name == "nt" or _sys.platform == "darwin"
    skipk = {s.casefold() for s in skip} if fold else skip
    out = set()
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        keep = []
        for d in dirnames:
            if (d.casefold() if fold else d) in skipk:
                continue
            out.add(os.path.join(dirpath, d))
            if not os.path.islink(os.path.join(dirpath, d)):
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if (f.casefold() if fold else f) in skipk:
                continue
            out.add(os.path.join(dirpath, f))
    return out


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259             # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        return wpid == 0
    except ChildProcessError:
        return True
