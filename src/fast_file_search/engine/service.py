"""The engine process (design B, C, D): owns the index DB, the in-memory index,
the user-data DB, the walker, the watcher and every search. No Qt, ever.

Threads
  rx (engine_main only)  conn.recv() -> Engine.handle()
  ffs-writer             the ONLY user of Store and the only caller of MemIndex.apply:
                         startup/load, scan listings -> diff -> op batches, watcher
                         batches, configure/index commands, snapshots, shutdown
  ffs-walker-N           os.scandir listings (walker.WalkerPool)
  ffs-search             latest-wins searches, pages, live refreshes, user-data ops
                         (opens and owns UserData)
  ffs-status             IndexStatus (<= 4/s), IndexChanged (<= 2/s), refresh trigger
  watchdog + ffs-watch-flush   (watcher.Watcher)

The spawn target is ``engine_main(conn, boot)``; ``Engine(boot, send)`` also runs
in-process (tests, ``--engine-inprocess``).
"""
from __future__ import annotations

import dataclasses
import errno
import importlib
import logging
import os
import queue
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

from . import protocol as P
from .exclusions import Exclusions, dedupe_roots, path_key
from .protocol import FOLDER, UNREADABLE
from .walker import (DESCEND, FLAG_MASK, NOSTAT, Activity, DirTask, Listing, WalkContext, WalkerPool,
                     entry_for_path, list_dir, same_volume_devices)

_log = logging.getLogger("fast_file_search.engine")

DB_NAME = "index-v5.db"
SNAP_NAME = "index-v5.snap"
LOCK_NAME = "engine.lock"
HISTORY_NAME = "search_history.db"

BATCH_ROWS = 20000
BATCH_ROWS_ACTIVE = 4000      # while the user types: short MemIndex lock holds
BATCH_ROWS_FIRST = 50000      # first scan of an empty DB (still closed by BATCH_SECS)
BATCH_SECS = 0.25
STATUS_MIN_INTERVAL = 0.25
STATUS_IDLE_INTERVAL = 5.0
CHANGED_MIN_INTERVAL = 0.5
REFRESH_MIN_INTERVAL = 1.0
PER_FILE_LIMIT = 64          # more touched names in one dir -> re-list the dir instead
# Watcher events reach the writer later than the disk changes they describe, so a
# batch can be applied after a folder it names has already been moved on disk (the
# move's own event follows in a later batch). Folders that had events applied this
# recently are re-walked after they move; see Engine._note_recent.
RECENT_EVENT_SECS = 15.0
RECENT_EVENT_MAX = 4096
IDLE_FOR_PERIODIC = 60.0     # no search for this long before a periodic rescan
LIMITED_RESCAN_MIN = 15

_CORRUPT_WORDS = ("malformed", "not a database", "disk image", "corrupt", "encrypted")


def _load_core():
    """P1's modules (or a stand-in named by ``FFS_ENGINE_CORE`` for tests)."""
    name = os.environ.get("FFS_ENGINE_CORE")
    if name:
        m = importlib.import_module(name)
        return SimpleNamespace(ops=m, Store=m.Store, StoreCorrupt=m.StoreCorrupt, MemIndex=m.MemIndex,
                               UserData=m.UserData, name=name)
    from . import memindex, ops, store, userdata
    return SimpleNamespace(ops=ops, Store=store.Store, StoreCorrupt=store.StoreCorrupt,
                           MemIndex=memindex.MemIndex, UserData=userdata.UserData, name="core")


def default_walker_threads(platform: str = sys.platform) -> int:
    env = os.environ.get("FFS_WALKER_THREADS")
    if env and env.isdigit():
        return max(1, int(env))
    return 1 if platform == "win32" else 4


# ---------------------------------------------------------------------------- lock file

class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self, timeout: float = 5.0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        end = time.monotonic() + timeout
        while True:
            fh = open(self.path, "a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh = fh
                try:
                    fh.truncate(0)
                    fh.write(str(os.getpid()).encode())
                    fh.flush()
                except OSError:
                    pass
                return True
            except OSError:
                fh.close()
                if time.monotonic() >= end:
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


# ---------------------------------------------------------------------------- scan state

class _Scan:
    """One walk: a root scan ('full' | 'structure'), a subtree rescan ('sub'), or the
    long-lived per-root watcher scan ('flush': one-level re-lists, new folders walked)."""

    def __init__(self, root_id: int, path: str, mode: str, background: bool, ctx: WalkContext,
                 first: bool = False):
        self.root_id = root_id
        self.path = path
        self.mode = mode
        self.background = background
        self.ctx = ctx
        self.first = first
        self.outstanding = 0
        self.entries = 0
        self.dirs = 0
        self.denied = 0
        self.cancelled = False
        self.failed: Optional[str] = None
        self.t0 = time.monotonic()

    @property
    def is_root_scan(self) -> bool:
        return self.mode in ("full", "structure")


class _RootInfo:
    def __init__(self, rid: int, path: str, state: str = "new", last_scan_end=None, complete: int = 0,
                 entries: int = 0, denied: int = 0):
        self.id = rid
        self.path = path
        self.state = state
        self.last_scan_end = last_scan_end
        self.complete = complete
        self.expected = entries
        self.denied = denied
        self.live = "off"
        self.scan: Optional[_Scan] = None
        self.flush_scan: Optional[_Scan] = None
        self.notified = set()
        self.needs_full = False
        self.last_scan_mono = time.monotonic() if last_scan_end else 0.0


def protected_folder_name(path: str, home: str, platform: str = sys.platform) -> str:
    """Display name of the macOS privacy-protected place ``path`` is in, or ''."""
    if platform != "darwin":
        return ""
    import posixpath
    k = path_key(path, platform)
    for sub, label in (("Desktop", "Desktop"), ("Documents", "Documents"), ("Downloads", "Downloads"),
                       ("Library/Mobile Documents", "iCloud Drive"), ("Library/CloudStorage", "cloud storage")):
        base = path_key(posixpath.join(home, sub), platform)
        if k == base or k.startswith(base + "/"):
            return label
    if k.startswith("/volumes/"):
        parts = path.split("/")
        return parts[2] if len(parts) > 2 else "the volume"
    return ""


def _display_names(paths: List[str]) -> List[str]:
    def base(p: str) -> str:
        b = os.path.basename(p.rstrip("\\/"))
        return b or p
    names = [base(p) for p in paths]
    out = []
    for p, n in zip(paths, names):
        if names.count(n) > 1:
            parent = os.path.dirname(p.rstrip("\\/"))
            out.append(f"{n} ({parent})" if parent else n)
        else:
            out.append(n)
    return out


def _root_counts(counters, rid: int):
    """(entries, folders, bytes) of one root from MemIndex.counters()."""
    roots = getattr(counters, "roots", None)
    if roots is None:
        return 0, 0, 0
    rc = roots.get(rid) if isinstance(roots, dict) else None
    if rc is None and not isinstance(roots, dict):
        for r in roots:
            if (getattr(r, "root_id", None) if not isinstance(r, tuple) or hasattr(r, "_fields") else r[0]) == rid:
                rc = r
                break
    if rc is None:
        return 0, 0, 0
    if hasattr(rc, "entries"):
        return rc.entries, getattr(rc, "folders", 0), getattr(rc, "bytes", 0)
    return rc[0], rc[1], rc[2]


def _outermost(paths: List[str]) -> List[str]:
    """``paths`` without duplicates and without paths below another one."""
    out: List[str] = []
    kept: List[str] = []
    for k, p in sorted((path_key(p), p) for p in paths):     # parents sort before children
        if any(k == a or k.startswith(a if a.endswith(os.sep) else a + os.sep) for a in kept):
            continue
        out.append(p)
        kept.append(k)
    return out


def _total(counters, *names, default=0):
    for n in names:
        v = getattr(counters, n, None)
        if v is not None:
            return v
    return default


# ---------------------------------------------------------------------------- search thread

class _SearchWorker:
    def __init__(self, eng: "Engine"):
        self.eng = eng
        self.cv = threading.Condition()
        self.latest: Optional[P.SearchRequest] = None
        self.latest_gen = -1
        self.pages: deque = deque()
        self.tasks: deque = deque()
        self.refresh_wanted = False
        self.last_req: Optional[P.SearchRequest] = None
        self.last_sig = None
        self.last_total = 0
        self.user = None
        self.user_ready = threading.Event()
        self.timings: deque = deque(maxlen=512)    # (search_ms, send_ms, rows)
        self._stop = False
        self.thread = threading.Thread(target=self._run, name="ffs-search", daemon=True)

    def submit(self, req: P.SearchRequest) -> None:
        with self.cv:
            self.latest = req
            self.latest_gen = req.gen
            self.cv.notify()

    def page(self, msg: P.FetchPage) -> None:
        with self.cv:
            self.pages.append(msg)
            self.cv.notify()

    def task(self, msg) -> None:
        with self.cv:
            self.tasks.append(msg)
            self.cv.notify()

    def request_refresh(self) -> None:
        with self.cv:
            self.refresh_wanted = True
            self.cv.notify()

    def stop(self) -> None:
        with self.cv:
            self._stop = True
            self.cv.notify_all()

    def _open_user(self) -> None:
        eng = self.eng
        try:
            Path(eng.boot.data_dir).mkdir(parents=True, exist_ok=True)
            self.user = eng.core.UserData(Path(eng.boot.data_dir) / HISTORY_NAME)
        except Exception as exc:
            _log.exception("user data unavailable")
            eng.notice("error", "history_unavailable", f"Search history could not be opened: {exc}")
            self.user = None
        self.user_ready.set()

    def _run(self) -> None:
        eng = self.eng
        if not eng._core_ready.wait(30) or eng.core is None:
            self.user_ready.set()
            return
        self._open_user()
        if not eng.wait_ready():
            self._close_user()
            return
        last_backup = 0.0
        while True:
            with self.cv:
                while not self._stop and self.latest is None and not self.tasks and not self.pages \
                        and not self.refresh_wanted:
                    if not self.cv.wait(60.0):
                        break
                if self._stop:
                    break
                req, self.latest = self.latest, None
                task = self.tasks.popleft() if req is None and self.tasks else None
                page = self.pages.popleft() if req is None and task is None and self.pages else None
                refresh = False
                if req is None and task is None and page is None and self.refresh_wanted:
                    self.refresh_wanted = False
                    refresh = True
            try:
                if req is not None:
                    try:
                        self._search(req)
                    finally:
                        if self.latest is None:
                            eng.activity.release()
                elif task is not None:
                    self._user_task(task)
                elif page is not None:
                    self._page(page)
                elif refresh:
                    self._refresh()
                now = time.monotonic()
                if self.user is not None and now - last_backup > 3600:
                    last_backup = now
                    try:
                        self.user.backup_if_due()
                    except Exception:
                        _log.debug("history backup failed", exc_info=True)
            except Exception as exc:
                _log.exception("search thread task failed")
                if req is not None:
                    eng.send(P.SearchResult(gen=req.gen, total=0, offset=0, rows=(), elapsed_ms=0.0,
                                            error=f"Search failed: {exc}"))
        self._close_user()

    def _close_user(self) -> None:
        u, self.user = self.user, None
        if u is not None:
            try:
                u.close()
            except Exception:
                pass

    @staticmethod
    def _sig(res: P.SearchResult):
        return res.total, tuple((r.id, r.size, r.mtime, r.favorite, r.name) for r in res.rows)

    def _search(self, req: P.SearchRequest, refresh: bool = False) -> None:
        eng = self.eng
        mem = eng.mem
        gen = req.gen
        cancel = (lambda: self.latest_gen != gen)
        t0 = time.perf_counter()
        res = mem.search(req, cancel, self.user)
        t1 = time.perf_counter()
        if res is None or self.latest_gen != gen:
            return
        sig = self._sig(res)
        if refresh and sig == self.last_sig:
            return
        res = dataclasses.replace(res, partial=eng.partial, refresh=refresh,
                                  index_generation=res.index_generation or mem.generation)
        eng.send(res)
        t2 = time.perf_counter()
        self.timings.append(((t1 - t0) * 1000, (t2 - t1) * 1000, len(res.rows)))
        self.last_req = req
        self.last_sig = sig
        self.last_total = res.total
        if req.commit_history and not refresh and self.user is not None and req.text.strip():
            try:
                self.user.record_search(req.text.strip(), res.total)
            except Exception:
                _log.debug("record_search failed", exc_info=True)

    def _refresh(self) -> None:
        req = self.last_req
        if req is None or self.latest_gen != req.gen:
            return
        self._search(dataclasses.replace(req, commit_history=False), refresh=True)

    def _page(self, msg: P.FetchPage) -> None:
        if msg.gen != self.latest_gen:
            return
        res = self.eng.mem.page(msg.gen, msg.offset, msg.limit)
        if res is not None and msg.gen == self.latest_gen:
            self.eng.send(dataclasses.replace(res, partial=self.eng.partial))

    def _user_task(self, msg) -> None:
        u = self.user
        eng = self.eng
        if isinstance(msg, P.ItemInfoRequest):
            fav, tags, note = False, (), ""
            if u is not None:
                try:
                    fav = msg.path in u.favorite_paths()
                    tags = tuple(u.tags(msg.path))
                    note = u.note(msg.path) or ""
                except Exception:
                    _log.debug("item info failed", exc_info=True)
            eng.send(P.ItemInfo(msg.req_id, msg.path, fav, tags, note))
            return
        if u is None:
            return
        op, paths, value = msg.op, tuple(msg.paths or ()), msg.value
        refresh = False
        if op in ("favorite", "unfavorite"):
            for p in paths:
                u.set_favorite(p, op == "favorite")
            refresh = True
        elif op == "set_tags":
            for p in paths:
                u.set_tags(p, list(value or ()))
        elif op == "set_note":
            for p in paths:
                u.set_note(p, value or "")
        elif op == "forget":
            u.forget(paths)
            refresh = True
        elif op == "clear_history":
            u.clear_history()
        elif op == "pin":
            u.pin(value)
        elif op == "unpin":
            u.unpin(value)
        elif op == "record_open":
            text = (value or "").strip()
            if text:
                count = self.last_total if self.last_req is not None and self.last_req.text.strip() == text else 1
                u.record_search(text, max(1, count))
        else:
            _log.warning("unknown user op %r", op)
        if refresh:
            self._refresh()


# ---------------------------------------------------------------------------- engine

class Engine:
    """The engine coordinator. ``send`` is called from engine threads with
    protocol objects (it must be thread-safe or tolerate a lock held around it)."""

    def __init__(self, boot: P.EngineBootstrap, send: Callable[[object], None]):
        self.boot = boot
        self.cfg: P.EngineConfig = boot.config
        self._send_raw = send
        self._send_lock = threading.Lock()
        self.platform = sys.platform
        self.sep = os.sep
        self.core = None
        self.store = None
        self.mem = None
        self.activity = Activity()
        self._wq: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._ready = threading.Event()
        self._core_ready = threading.Event()
        self.dead = threading.Event()            # set when the engine cannot continue (fatal/stopped)
        self._roots: Dict[int, _RootInfo] = {}
        self._root_order: List[int] = []
        self._ops: list = []
        self._t_first_op: Optional[float] = None
        self._touched: Dict[int, float] = {}
        self._relisted: set = set()
        # path_key(dir) -> monotonic time a watcher event at or directly below it was applied
        self._recent_dirs: Dict[str, float] = {}
        self._gen = 0
        self._snap_gen = -1
        self._first_complete_pending = False
        self.partial = False
        self.capabilities: tuple = ()
        self.pool: Optional[WalkerPool] = None
        self.watcher = None
        self.power = None
        self._assertion = None
        self.low_power = False
        self._status_dirty = True
        self._last_status_t = 0.0
        self._sent_gen = -1
        self._last_changed_t = 0.0
        self._refresh_gen = -1
        self._last_refresh_t = 0.0
        self._last_housekeeping = 0.0
        self._last_compact = time.monotonic()
        self._lock_file: Optional[_FileLock] = None
        self._fast_writes = False
        self._warm_pending = True
        self._snapshot_due = False
        self._stopping = False
        self._fatal_sent = False
        self.ex = self._make_exclusions(self.cfg)
        self.search = _SearchWorker(self)
        self._writer = threading.Thread(target=self._writer_main, name="ffs-writer", daemon=True)
        self._status = threading.Thread(target=self._status_main, name="ffs-status", daemon=True)
        self.t_created = time.perf_counter()
        self.t_ready: Optional[float] = None
        self.stats = SimpleNamespace(listings=0, commits=0, ops=0, batches=0, batch_ops=0, flush_s=0.0)
        self._diag = deque(maxlen=4000) if os.environ.get("FFS_DIAG") else None

    def _d(self, *a) -> None:
        if self._diag is not None:
            self._diag.append((round(time.monotonic(), 3), self.stats.commits) + a)

    # ------------------------------------------------------------------ public API
    def start(self) -> None:
        self._writer.start()
        self.search.thread.start()
        self._status.start()

    def handle(self, msg: object) -> None:
        if isinstance(msg, P.SearchRequest):
            self.activity.poke()
            self.search.submit(msg)
        elif isinstance(msg, P.FetchPage):
            self.search.page(msg)
        elif isinstance(msg, (P.UserOp, P.ItemInfoRequest)):
            self.search.task(msg)
        elif isinstance(msg, P.Configure):
            self._wq.put(("configure", msg.config))
        elif isinstance(msg, P.IndexCommand):
            self._wq.put(("index", msg))
        elif isinstance(msg, P.Hello):
            if msg.protocol_version != P.PROTOCOL_VERSION:
                self._fatal(f"Protocol mismatch: GUI {msg.protocol_version}, engine {P.PROTOCOL_VERSION}", "")
        elif isinstance(msg, P.Shutdown):
            threading.Thread(target=self.stop, name="ffs-stop", daemon=True).start()
        else:
            _log.warning("unknown message %r", type(msg).__name__)

    def stop(self, timeout: float = 3.0) -> None:
        if self._stopping:
            self._stopped.wait(timeout)
            return
        self._stopping = True
        end = time.monotonic() + timeout
        self._stop.set()
        self._ready.set()
        self._core_ready.set()
        try:
            if self.watcher is not None:
                self.watcher.stop()
        except Exception:
            _log.debug("watcher stop failed", exc_info=True)
        if self.pool is not None:
            self.pool.stop()
        if self.power is not None:
            self.power.stop()
        self._wq.put(("stop",))
        if self._writer.is_alive() and threading.current_thread() is not self._writer:
            self._writer.join(max(0.1, end - time.monotonic()))
        self.search.stop()
        if self.search.thread.is_alive():
            self.search.thread.join(max(0.1, min(1.0, end - time.monotonic())))
        self.dead.set()
        self._stopped.set()

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        self._ready.wait(timeout)
        return self._ready.is_set() and not self._stop.is_set()

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and self.mem is not None

    def send(self, msg: object) -> None:
        with self._send_lock:
            try:
                self._send_raw(msg)
            except (OSError, EOFError, ValueError) as exc:
                if not self._stop.is_set():
                    _log.info("GUI connection lost (%s); stopping", exc)
                    threading.Thread(target=self.stop, name="ffs-stop", daemon=True).start()

    def notice(self, level: str, code: str, text: str, **data) -> None:
        self.send(P.Notice(level, code, text, tuple(sorted(data.items()))))

    def _fatal(self, text: str, tb: str) -> None:
        if not self._fatal_sent:
            self._fatal_sent = True
            _log.critical("engine fatal: %s\n%s", text, tb)
            self.send(P.Fatal(text, tb))
        self.dead.set()
        threading.Thread(target=self.stop, name="ffs-stop", daemon=True).start()

    # ------------------------------------------------------------------ config
    def _make_exclusions(self, cfg: P.EngineConfig) -> Exclusions:
        b = self.boot
        own_dirs = [b.cache_dir, b.data_dir, b.log_dir, *b.own_paths]
        own_files = [os.path.join(b.cache_dir, n) for n in (DB_NAME, SNAP_NAME, LOCK_NAME)]
        own_files.append(os.path.join(b.data_dir, HISTORY_NAME))
        home = os.path.expanduser("~")
        return Exclusions(cfg.exclude_patterns, home=home if home != "~" else None, own_dirs=own_dirs,
                          own_files=own_files, include_home_library=cfg.include_home_library,
                          index_hidden=cfg.index_hidden, descend_packages=cfg.descend_packages,
                          builtin=os.environ.get("FFS_NO_BUILTIN_EXCLUDES") != "1")

    def _config_roots(self, cfg: P.EngineConfig) -> List[str]:
        return dedupe_roots([r for r in cfg.roots if r])

    # ------------------------------------------------------------------ writer thread
    def _writer_main(self) -> None:
        try:
            self._open()
        except Exception as exc:
            self._core_ready.set()
            self._fatal(f"The index could not be opened: {exc}", traceback.format_exc())
            self._close_store(clean=False)
            return
        try:
            self._after_ready()
            self._writer_loop()
        except Exception as exc:
            self._fatal(f"Index writer failed: {exc}", traceback.format_exc())
        finally:
            self._shutdown_writer()

    def _open(self) -> None:
        t0 = time.perf_counter()
        boot = self.boot
        cache = Path(boot.cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        self.core = _load_core()
        self._core_ready.set()
        self._lock_file = _FileLock(cache / LOCK_NAME)
        if not self._lock_file.acquire(timeout=5.0):
            self._lock_file = None
            raise RuntimeError("another engine is still running (engine.lock is held)")
        db = cache / DB_NAME
        snap = cache / SNAP_NAME
        store, recovered = self.core.Store.open_or_recover(db)
        self.store = store
        try:
            store.mark_open()
        except Exception:
            _log.debug("mark_open failed", exc_info=True)
        caps = []
        mem = None
        if recovered:
            self._drop_snapshot()
        elif snap.exists():
            try:
                mem = self.core.MemIndex.load_snapshot(snap, store.generation)
            except Exception:
                _log.warning("snapshot load failed", exc_info=True)
                mem = None
            if mem is None:
                self._drop_snapshot()
            else:
                caps.append("snapshot")
        if mem is None:
            mem = self.core.MemIndex.build(store)
        self.mem = mem
        self._gen = store.generation
        self._snap_gen = store.generation if "snapshot" in caps else -1
        self._reconcile_roots(self._config_roots(self.cfg))
        self.search.user_ready.wait(10)
        try:
            import sqlite3
            caps.append(f"sqlite:{sqlite3.sqlite_version}")
        except Exception:
            pass
        caps.append({"win32": "watchdog:windows", "darwin": "watchdog:fsevents"}.get(sys.platform,
                                                                                    "watchdog:inotify"))
        caps.append(f"qt_modules:{sum(1 for m in sys.modules if m == 'PySide6' or m.startswith('PySide6.'))}")
        caps.append(f"core:{self.core.name}")
        caps.append(f"walker_threads:{default_walker_threads()}")
        self.capabilities = tuple(caps)
        self.t_ready = time.perf_counter()
        _log.info("engine ready in %.0f ms (%s)", (self.t_ready - t0) * 1000, ", ".join(caps))
        self._ready.set()
        self.send(P.Ready(P.PROTOCOL_VERSION, self._status_msg(), self.capabilities))
        self._last_status_t = time.monotonic()
        self._status_dirty = False
        if recovered:
            self.notice("warning", "index_rebuilt", "The index was damaged and is being rebuilt.")

    def _after_ready(self) -> None:
        from .power import PowerMonitor
        self.pool = WalkerPool(self._deliver, self.activity, nthreads=default_walker_threads(), max_inflight=256)
        try:
            self.power = PowerMonitor(lambda s: self._wq.put(("power", s)))
            self._set_power(self.power.state, initial=True)
        except Exception:
            _log.debug("power monitor unavailable", exc_info=True)
        if sys.platform == "darwin":
            try:
                from .macos import ActivityAssertion
                self._assertion = ActivityAssertion()
            except Exception:
                self._assertion = None
        self._start_watcher()
        self._launch_scans()

    def _set_fast_writes(self, on: bool) -> None:
        fn = getattr(self.store, "set_fast_writes", None)
        if fn is None or self._fast_writes == on:
            return
        try:
            fn(on)
            self._fast_writes = on
        except Exception:
            _log.debug("set_fast_writes failed", exc_info=True)

    def _launch_scans(self) -> None:
        try:
            empty = _total(self.mem.counters(), "total_entries", "entries", "total") == 0
        except Exception:
            empty = False
        if empty and self._root_order:
            self._set_fast_writes(True)      # first scan of an empty DB: synchronous=OFF
        for rid in self._root_order:
            ri = self._roots[rid]
            if ri.state == "new" or not ri.complete:
                self._start_scan(rid, "full", background=False)
            elif self.low_power and self.cfg.pause_on_battery:
                ri.needs_full = True
                self._start_scan(rid, "structure", background=True)
            else:
                self._start_scan(rid, "full", background=True)

    def _writer_loop(self) -> None:
        wq = self._wq
        while not self._stop.is_set():
            if self._ops:
                timeout = max(0.0, (self._t_first_op or time.monotonic()) + BATCH_SECS - time.monotonic())
            elif self._warm_pending or self._snapshot_due:
                timeout = 0.02
            else:
                timeout = 0.5
            try:
                item = wq.get(timeout=timeout)
            except queue.Empty:
                item = None
                if not self._ops:
                    if self._snapshot_due and not self.activity.recently_active(2.0) and not self._scans_active():
                        self._snapshot_due = False
                        self._save_snapshot()
                    elif self._warm_pending:
                        self._warm_step()
            if item is not None:
                if item[0] == "stop":
                    break
                if item[0] == "L":
                    self.activity.wait(self._stop)
                try:
                    self._dispatch(item)
                except Exception as exc:
                    if self._is_corrupt(exc):
                        self._recover_store(exc)
                    else:
                        raise
                if self._ops and self._t_first_op is None:
                    self._t_first_op = time.monotonic()
            if self._ops:
                now = time.monotonic()
                # smaller batches while the user types: MemIndex.apply holds the index lock
                limit = (BATCH_ROWS_ACTIVE if self.activity.recently_active()
                         else BATCH_ROWS_FIRST if self._fast_writes else BATCH_ROWS)
                if (len(self._ops) >= limit or now - self._t_first_op >= BATCH_SECS
                        or (wq.empty() and self.pool.idle())):
                    self.activity.wait(self._stop)
                    self._commit_safe()
            now = time.monotonic()
            if now - self._last_housekeeping >= 0.5:
                self._last_housekeeping = now
                self._housekeeping(now)

    def _warm_step(self) -> None:
        """MemIndex.warm(): precompute lazy columns in small slices while idle, so the
        first abbreviation-tier query does not pay for them."""
        if self.activity.recently_active(0.2) or self._scans_active():
            return
        fn = getattr(self.mem, "warm", None)
        if fn is None:
            self._warm_pending = False
            return
        try:
            self._warm_pending = not fn(0.02)
        except Exception:
            _log.debug("warm failed", exc_info=True)
            self._warm_pending = False

    def _dispatch(self, item) -> None:
        kind = item[0]
        if kind == "L":
            self.pool.done()
            self._on_listing(item[1])
        elif kind == "batch":
            self._apply_batch(item[1])
        elif kind == "configure":
            self._configure(item[1])
        elif kind == "index":
            self._index_command(item[1])
        elif kind == "power":
            self._set_power(item[1])
        elif kind == "call":
            item[1]()

    def _deliver(self, lst: Listing) -> None:
        self._wq.put(("L", lst))

    # ------------------------------------------------------------------ commits
    def _commit_safe(self) -> None:
        try:
            self._commit()
        except Exception as exc:
            if self._is_corrupt(exc):
                self._recover_store(exc)
            else:
                raise

    def _commit(self) -> None:
        if not self._ops:
            self._t_first_op = None
            return
        ops, self._ops = self._ops, []
        self._t_first_op = None
        try:
            gen = self.store.apply(ops)
        except Exception as exc:
            if self._diag is not None:
                self._dump_diag(ops, exc)
            raise
        self.mem.apply(ops, gen)
        self._gen = gen
        self._warm_pending = True
        self.stats.commits += 1
        self.stats.ops += len(ops)
        self._status_dirty = True

    def _dump_diag(self, ops, exc) -> None:
        O = self.core.ops
        seen = {}
        dups = []
        for o in ops:
            if o[0] == O.INS:
                k = (o[2], o[3])
                if k in seen:
                    dups.append((seen[k], o))
                seen[k] = o
                ex = self.store.entry_lookup(o[2], o[3])
                if ex is not None:
                    dups.append(("STORE", ex, o))
        lines = [f"DIAG commit failed: {exc}; {len(ops)} ops; dups={dups[:20]}"]
        ids = {o[2] for d in dups for o in d if isinstance(o, tuple) and len(o) > 3}
        for i in ids:
            lines.append(f"DIAG dir {i}: {self.store.dir_row(i)}")
        for t in self._diag:
            lines.append("DIAG " + repr(t))
        _log.critical(chr(10).join(lines))
        print(chr(10).join(lines), file=sys.stderr, flush=True)

    def _commit_root_state(self, rid: int, **fields) -> None:
        self._commit()
        try:
            self.store.set_root_state(rid, **fields)
            gen = self.store.apply([])
            self.mem.apply([], gen)
            self._gen = gen
        except Exception as exc:
            if self._is_corrupt(exc):
                raise
            _log.warning("root state update failed: %s", exc)

    @staticmethod
    def _is_corrupt(exc: BaseException) -> bool:
        name = type(exc).__name__
        if name not in ("DatabaseError", "StoreCorrupt") and not name.endswith("DatabaseError"):
            return False
        text = str(exc).lower()
        return name == "StoreCorrupt" or any(w in text for w in _CORRUPT_WORDS)

    def _recover_store(self, exc: BaseException) -> None:
        _log.error("index DB corrupt (%s): quarantining and rebuilding", exc)
        self._ops = []
        self._cancel_scans()
        try:
            self.store.close(clean=False)
        except Exception:
            pass
        db = Path(self.boot.cache_dir) / DB_NAME
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            os.replace(db, db.with_name(f"{DB_NAME}.corrupt-{stamp}"))
        except OSError:
            pass
        for sfx in ("-wal", "-shm"):
            try:
                os.remove(str(db) + sfx)
            except OSError:
                pass
        corrupt = sorted(db.parent.glob(f"{DB_NAME}.corrupt-*"))
        for old in corrupt[:-2]:
            try:
                old.unlink()
            except OSError:
                pass
        self._drop_snapshot()
        self.store, _ = self.core.Store.open_or_recover(db)
        self.store.mark_open()
        self._reset_index_from_store()
        self.notice("warning", "index_rebuilt", "The index was damaged and is being rebuilt.")
        self._launch_scans()

    def _reset_index_from_store(self) -> None:
        """New store (rebuild/recovery): re-add the configured roots and swap the MemIndex."""
        roots = [self._roots[r].path for r in self._root_order] or self._config_roots(self.cfg)
        self._roots.clear()
        self._root_order = []
        self.mem = self.core.MemIndex.build(self.store)
        self._gen = self.store.generation
        self._snap_gen = -1
        self._reconcile_roots(roots)
        self._start_watcher()

    def _drop_snapshot(self) -> None:
        try:
            os.remove(Path(self.boot.cache_dir) / SNAP_NAME)
        except OSError:
            pass

    def _save_snapshot(self) -> None:
        if self.mem is None or self._gen == self._snap_gen:
            return
        try:
            t0 = time.perf_counter()
            self.mem.save_snapshot(Path(self.boot.cache_dir) / SNAP_NAME)
            self._snap_gen = self._gen
            _log.info("snapshot saved in %.0f ms (generation %d)", (time.perf_counter() - t0) * 1000, self._gen)
        except Exception:
            _log.warning("snapshot save failed", exc_info=True)
            self._drop_snapshot()

    def _close_store(self, clean: bool) -> None:
        st, self.store = self.store, None
        self._fast_writes = False
        if st is not None:
            try:
                st.close(clean=clean)
            except Exception:
                _log.debug("store close failed", exc_info=True)
        if self._lock_file is not None:
            self._lock_file.release()
            self._lock_file = None

    def _shutdown_writer(self) -> None:
        try:
            if self.store is not None and self.mem is not None:
                try:
                    self._commit()
                except Exception:
                    _log.warning("final commit failed", exc_info=True)
                self._save_snapshot()
        finally:
            if self._assertion is not None:
                self._assertion.set_active(False)
            self._close_store(clean=True)

    # ------------------------------------------------------------------ roots
    def _reconcile_roots(self, wanted: List[str]) -> List[int]:
        """Make the store's roots equal ``wanted`` (by path key). Returns new root ids."""
        store = self.store
        existing = {path_key(r.path): r for r in store.roots()}
        want_keys = [path_key(p) for p in wanted]
        removed = [r for k, r in existing.items() if k not in want_keys]
        if removed:
            self._commit()
            for r in removed:
                ri = self._roots.pop(r.id, None)
                if ri is not None and ri.scan is not None:
                    ri.scan.cancelled = True
                if ri is not None and ri.flush_scan is not None:
                    ri.flush_scan.cancelled = True
            if self.pool is not None:
                gone = {r.id for r in removed}
                self.pool.remove(lambda t: t.scan.root_id in gone)
            ops = [(self.core.ops.ROOTDEL, r.id) for r in removed]
            gen = store.apply(ops)
            self.mem.apply(ops, gen)
            self._gen = gen
        new_ids = []
        order = []
        for p, k in zip(wanted, want_keys):
            r = existing.get(k)
            if r is None:
                rid = store.add_root(p)
                # add_root wrote the root's dirs row itself; mirror it into the MemIndex
                self.mem.apply([(self.core.ops.DIR, rid, 0, rid, p, 0)], store.generation)
                ri = _RootInfo(rid, p, "new")
                new_ids.append(rid)
            else:
                rid = r.id
                ri = self._roots.get(rid) or _RootInfo(rid, r.path, r.state, r.last_scan_end,
                                                       r.last_scan_complete, r.entries, r.denied_dirs)
            self._roots[rid] = ri
            order.append(rid)
        self._root_order = order
        if new_ids:
            self._gen = store.generation
        self._status_dirty = True
        return new_ids

    def _configure(self, cfg: P.EngineConfig) -> None:
        old = self.cfg
        self.cfg = cfg
        excl_changed = (old.exclude_patterns != cfg.exclude_patterns or old.include_home_library !=
                        cfg.include_home_library or old.index_hidden != cfg.index_hidden or
                        old.descend_packages != cfg.descend_packages)
        if excl_changed:
            self.ex = self._make_exclusions(cfg)
        self._commit()
        before = set(self._root_order)
        new_ids = self._reconcile_roots(self._config_roots(cfg))
        if set(self._root_order) != before or old.live_monitoring != cfg.live_monitoring or excl_changed:
            self._start_watcher()
        for rid in self._root_order:
            if rid in new_ids:
                self._start_scan(rid, "full", background=False)
            elif excl_changed:
                self._start_scan(rid, "full", background=False)
        if old.pause_on_battery != cfg.pause_on_battery and self.power is not None:
            self._set_power(self.power.state)

    def _index_command(self, cmd: P.IndexCommand) -> None:
        if cmd.action == "rebuild":
            self._rebuild()
            return
        ids = list(cmd.root_ids) if cmd.root_ids else list(self._root_order)
        for rid in ids:
            if rid in self._roots:
                self._start_scan(rid, "full", background=False)

    def _rebuild(self) -> None:
        _log.info("rebuilding the index")
        self._cancel_scans()
        self._ops = []
        self._close_store(clean=True)
        cache = Path(self.boot.cache_dir)
        for sfx in ("", "-wal", "-shm"):
            try:
                os.remove(str(cache / DB_NAME) + sfx)
            except OSError:
                pass
        self._drop_snapshot()
        self._lock_file = _FileLock(cache / LOCK_NAME)
        self._lock_file.acquire(timeout=5.0)
        self.store, _ = self.core.Store.open_or_recover(cache / DB_NAME)
        self.store.mark_open()
        self._reset_index_from_store()
        self._launch_scans()

    def _cancel_scans(self) -> None:
        for ri in self._roots.values():
            for s in (ri.scan, ri.flush_scan):
                if s is not None:
                    s.cancelled = True
            ri.scan = None
            ri.flush_scan = None
        if self.pool is not None:
            self.pool.remove(lambda t: True)
        self._touched.clear()

    # ------------------------------------------------------------------ scans
    def _ctx(self, ri: _RootInfo) -> WalkContext:
        return WalkContext(self.ex, devices=same_volume_devices(ri.path), root=ri.path)

    def _start_scan(self, rid: int, mode: str, background: bool) -> Optional[_Scan]:
        ri = self._roots.get(rid)
        if ri is None or self.pool is None:
            return None
        if ri.scan is not None and not ri.scan.cancelled:
            old = ri.scan
            if old.ctx.ex is self.ex and (old.mode == mode or (old.mode == "full" and mode == "structure")):
                if not background:
                    old.background = False   # a user request upgrades the running scan
                return old
            old.cancelled = True
            self.pool.remove(lambda t, o=old: t.scan is o)
        first = ri.state == "new" or not ri.complete
        scan = _Scan(rid, ri.path, mode, background, self._ctx(ri), first=first)
        ri.scan = scan
        if mode == "full":
            ri.needs_full = False
        ri.state = "scanning"
        self._commit()
        try:
            self.store.set_root_state(rid, state="scanning", last_scan_start=int(time.time()),
                                      last_scan_complete=0 if first else ri.complete)
        except Exception:
            _log.debug("set_root_state failed", exc_info=True)
        scan.outstanding = 1
        self._update_partial()
        self._set_assertion()
        self.pool.push([DirTask(rid, ri.path, 0, False, (), scan)])
        self._status_dirty = True
        return scan

    def _sub_scan(self, rid: int, dir_id: int, path: str) -> None:
        """Incremental walk of one subtree (watcher RECURSIVE / overflow / dir move)."""
        ri = self._roots.get(rid)
        if ri is None or self.pool is None:
            return
        scan = _Scan(rid, ri.path, "sub", False, self._ctx(ri))
        self._d("sub_scan", id(scan), dir_id, path)
        scan.outstanding = 1
        self.pool.push([DirTask(dir_id, path, 1, False, (), scan)])

    def _flush_scan(self, rid: int) -> Optional[_Scan]:
        ri = self._roots.get(rid)
        if ri is None:
            return None
        if ri.flush_scan is None or ri.flush_scan.cancelled or ri.flush_scan.ctx.ex is not self.ex:
            ri.flush_scan = _Scan(rid, ri.path, "flush", False, self._ctx(ri))
        return ri.flush_scan

    def _update_partial(self) -> None:
        self.partial = any(ri.scan is not None and not ri.scan.cancelled and ri.scan.first
                           for ri in self._roots.values())

    def _set_assertion(self) -> None:
        if self._assertion is not None:
            active = any(ri.scan is not None for ri in self._roots.values())
            try:
                self._assertion.set_active(active)
            except Exception:
                pass

    def _scans_active(self) -> bool:
        return any(ri.scan is not None for ri in self._roots.values()) or (
            self.pool is not None and not self.pool.idle())

    def _on_listing(self, lst: Listing) -> None:
        task = lst.task
        scan = task.scan
        self.stats.listings += 1
        if scan.cancelled:
            self._task_done(scan, 0)
            return
        is_new = task.is_new
        self._d("L", scan.mode, id(scan), task.dir_id, task.path, is_new, lst.t_listed,
                self._touched.get(task.dir_id), None if lst.entries is None else len(lst.entries))
        if self._touched:
            t_touch = self._touched.get(task.dir_id)
            if t_touch is not None:
                if lst.t_listed < t_touch:
                    # listed before a watcher update of this dir: list it again
                    self.pool.push([task._replace(is_new=False)])
                    return
                is_new = False
        if lst.error is not None:
            self._on_list_error(task, lst.error)
            n = 0
        else:
            children = self._diff(task, lst.entries, is_new)
            if children:
                self.pool.push(children)
            n = len(children)
        self._task_done(scan, n)

    def _task_done(self, scan: _Scan, nchildren: int) -> None:
        scan.outstanding += nchildren - 1
        if scan.outstanding <= 0 and scan.mode != "flush":
            self._finish_scan(scan)

    def _diff(self, task: DirTask, entries: list, is_new: bool, old: Optional[dict] = None,
              one_level: Optional[bool] = None) -> List[DirTask]:
        """Ops for one listing against the stored children; returns child tasks."""
        ops = self._ops
        append = ops.append
        O = self.core.ops
        INS, UPD, DEL, DIR, DIRMTIME = O.INS, O.UPD, O.DEL, O.DIR, O.DIRMTIME
        scan = task.scan
        store = self.store
        did = task.dir_id
        rid = scan.root_id
        depth = task.depth + 1
        mode = scan.mode
        if one_level is None:
            one_level = mode == "flush"
        structure = mode == "structure"
        children: List[DirTask] = []
        self._d("diff", mode, did, is_new, old is not None, len(entries))
        scan.dirs += 1
        scan.entries += len(entries)
        if task.entry and task.entry[3] & UNREADABLE and old is None:
            sz, mt, ct, fl = task.entry
            append((UPD, did, sz, mt, ct, fl & ~UNREADABLE))
            append((DIRMTIME, did, mt, 0))
        if is_new:
            new = entries
        else:
            if old is None:
                old = {c[1]: c for c in store.children(did)}
            new = []
            for ent in entries:
                o = old.pop(ent[0], None)
                fl = ent[4]
                if o is None:
                    if not fl & NOSTAT:
                        new.append(ent)
                    continue
                if fl & NOSTAT:
                    continue                      # exists but unreadable: keep what we had
                f = fl & FLAG_MASK
                oid, _n, osz, omt, oct_, ofl = o
                if (ofl ^ f) & FOLDER:
                    append((DEL, oid))            # a file became a folder (or back)
                    new.append(ent)
                    continue
                unr = ofl & UNREADABLE
                if osz != ent[1] or omt != ent[2] or oct_ != ent[3] or (ofl & ~UNREADABLE) != f:
                    append((UPD, oid, ent[1], ent[2], ent[3], f | unr))
                if f & FOLDER:
                    if fl & DESCEND:
                        if one_level or (structure and omt == ent[2] and not unr):
                            continue
                        children.append(DirTask(oid, ent[5], depth, False, (ent[1], ent[2], ent[3], f | unr),
                                                scan))
                    elif not one_level:
                        # no longer descended (package, mount, loop): drop stale children
                        for k in store.children(oid):
                            append((DEL, k[0]))
            for o in old.values():
                append((DEL, o[0]))
        if new:
            ids = store.allocate_ids(len(new))
            for i, ent in zip(ids, new):
                name, size, mtime, ctime, fl, path = ent
                if fl & NOSTAT:
                    continue
                f = fl & FLAG_MASK
                append((INS, i, did, name, size, mtime, ctime, f))
                if path is not None:
                    append((DIR, i, did, rid, path, mtime))
                    if fl & DESCEND:
                        children.append(DirTask(i, path, depth, True, (size, mtime, ctime, f), scan))
        return children

    def _on_list_error(self, task: DirTask, exc: OSError) -> None:
        scan = task.scan
        ri = self._roots.get(scan.root_id)
        denied = isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM)
        missing = isinstance(exc, (FileNotFoundError, NotADirectoryError))
        if task.depth == 0 and scan.is_root_scan:
            scan.failed = "denied" if denied else "missing" if missing else "error"
            if ri is not None:
                code = {"denied": "tcc_denied", "missing": "root_missing"}.get(scan.failed, "root_error")
                if code not in ri.notified:
                    ri.notified.add(code)
                    text = {"tcc_denied": f"Permission to read {ri.path} was denied.",
                            "root_missing": f"{ri.path} is not available."}.get(code, f"{ri.path} could not be "
                                                                                       f"read: {exc}")
                    self.notice("warning", code, text, root_id=ri.id, path=ri.path)
            return
        if denied:
            scan.denied += 1
            if task.entry and not task.entry[3] & UNREADABLE:
                O = self.core.ops
                sz, mt, ct, fl = task.entry
                self._ops.append((O.UPD, task.dir_id, sz, mt, ct, fl | UNREADABLE))
                self._ops.append((O.DIRMTIME, task.dir_id, mt, UNREADABLE))
            if ri is not None and "denied_dirs" not in ri.notified and scan.is_root_scan:
                ri.notified.add("denied_dirs")
                self.notice("info", "denied_dirs", f"Some folders in {ri.path} could not be read.",
                            root_id=ri.id)
        elif not missing:
            _log.debug("cannot list %s: %s", task.path, exc)

    def _finish_scan(self, scan: _Scan) -> None:
        ri = self._roots.get(scan.root_id)
        if ri is None or ri.scan is not scan:
            return
        self._commit()
        ri.scan = None
        if scan.cancelled:
            self._update_partial()
            self._set_assertion()
            return
        dt = time.monotonic() - scan.t0
        if scan.failed:
            ri.state = scan.failed
            self._commit_root_state(ri.id, state=scan.failed, last_scan_end=int(time.time()))
        else:
            e, f, b = _root_counts(self.mem.counters(), ri.id)
            ri.state = "ok"
            ri.complete = 1
            ri.last_scan_end = int(time.time())
            ri.last_scan_mono = time.monotonic()
            ri.expected = e
            ri.denied = scan.denied
            self._commit_root_state(ri.id, state="ok", last_scan_end=ri.last_scan_end, last_scan_complete=1,
                                    entries=e, folders=f, bytes=b, denied_dirs=scan.denied)
            _log.info("scan of %s (%s) done: %d entries, %d dirs in %.2f s (%.0f entries/s)", ri.path,
                      scan.mode, scan.entries, scan.dirs, dt, scan.entries / dt if dt > 0 else 0)
        if self._fast_writes and not any(r.scan is not None and r.scan.first for r in self._roots.values()):
            self._set_fast_writes(False)
        self._update_partial()
        self._set_assertion()
        all_done = not any(r.scan is not None for r in self._roots.values())
        # report the finished scan now (the snapshot below holds the MemIndex lock for a while)
        self._last_status_t = time.monotonic()
        self._status_dirty = False
        self.send(self._status_msg())
        if all_done:
            self._touched.clear()
            if scan.first or self._snap_gen < 0:
                self._snapshot_due = True     # saved at the next quiet moment (it holds the index lock)

    # ------------------------------------------------------------------ watcher
    def _start_watcher(self) -> None:
        if self.watcher is not None:
            try:
                self.watcher.stop()
            except Exception:
                pass
            self.watcher = None
        roots = [self._roots[r].path for r in self._root_order]
        for ri in self._roots.values():
            ri.live = "off"
        if not self.cfg.live_monitoring or not roots:
            return
        try:
            from .watcher import Watcher
            w = Watcher(self.ex, lambda b: self._wq.put(("batch", b)),
                        on_error=lambda root, exc: _log.warning("watch %s failed: %s", root, exc))
            w.set_roots(roots)
            live = w.start()
        except Exception:
            _log.warning("live monitoring unavailable", exc_info=True)
            return
        self.watcher = w
        by_key = {path_key(p): s for p, s in live.items()}
        limited = False
        for ri in self._roots.values():
            ri.live = by_key.get(path_key(ri.path), "off")
            if ri.live == "limited":
                limited = True
        if limited and "inotify_limit" not in getattr(self, "_notified", set()):
            self._notified = {"inotify_limit"}
            self.notice("warning", "inotify_limit", "Live monitoring is limited: the system's file watch "
                        "limit was reached (fs.inotify.max_user_watches). Folders are rescanned every 15 minutes.")
        self._status_dirty = True

    def _root_for_path(self, path: str) -> Optional[_RootInfo]:
        k = path_key(path)
        best = None
        for ri in self._roots.values():
            rk = path_key(ri.path)
            if k == rk or k.startswith(rk if rk.endswith(os.sep) else rk + os.sep):
                if best is None or len(ri.path) > len(best.path):
                    best = ri
        return best

    @staticmethod
    def _split(path: str):
        parent, name = os.path.split(path)
        return parent, name

    def _apply_batch(self, batch) -> None:
        """One watcher batch. Planned first so every directory is re-listed at most
        once and no diff ever reads state that an uncommitted op already changed."""
        from .watcher import DIR as B_DIR, FILE as B_FILE, RECURSIVE as B_REC
        t0 = time.perf_counter()
        self._commit()
        n_before = self.stats.ops
        walking = self._scans_active()
        self._d("batch", walking, batch.collapsed, dict(batch.paths), list(batch.moves))
        now = time.monotonic()
        for ri in self._roots.values():
            if ri.flush_scan is not None:
                ri.flush_scan.ctx.visited.clear()   # loop protection is per walk, not forever
        if batch.collapsed:
            for p in batch.paths:
                self._rescan_path(p)
            self.stats.batches += 1
            return
        relist: Dict[int, tuple] = {}           # dir_id -> (path, root_id)
        unknown: List[str] = []                 # paths whose dir is not indexed (yet)
        self._relisted = set()                  # each dir is re-listed at most once per batch
        paths = batch.paths
        moved_dsts: List[str] = []              # moved dirs to re-walk (stale events before the move)
        noted: List[str] = []                   # parent dirs of this batch's paths (final spelling)
        if batch.moves:
            moves = batch.moves
            for i, (src, dst, is_dir) in enumerate(moves):
                # A later move of a directory that contains ``dst`` has already happened on
                # disk, so ``dst`` is only reachable at its final spelling (the index learns
                # the new directory path when that later move is applied).
                for s2, d2, dir2 in moves[i + 1:]:
                    if dir2 and dst.startswith(s2) and dst[len(s2):len(s2) + 1] in ("\\", "/"):
                        dst = d2 + dst[len(s2):]
                if is_dir and self._moved_recent(src, dst):
                    moved_dsts.append(dst)
                noted.append(os.path.dirname(src))
                noted.append(os.path.dirname(dst))
                self._move(src, dst, is_dir, walking, now, relist, unknown)
                if is_dir:
                    # later moves in this batch may name paths below the new directory
                    # spelling: the store has to know it before they are looked up
                    self._commit()
            self._commit()
            paths = self._rebase_paths(paths, batch.moves)
        by_parent: Dict[str, set] = {}
        rescans: List[str] = []
        for p, bits in paths.items():
            if bits & B_REC:
                rescans.append(p)
                continue
            noted.append(os.path.dirname(p))
            if bits & B_DIR:
                noted.append(p)
                self._plan_relist(p, relist, unknown)
            if bits & B_FILE:
                ri = self._root_for_path(p)
                if ri is not None and path_key(p) == path_key(ri.path):
                    relist[ri.id] = (ri.path, ri.id)
                    continue
                parent, name = self._split(p)
                by_parent.setdefault(parent, set()).add(name)
        perfile: Dict[int, tuple] = {}
        for parent, names in by_parent.items():
            look = self.store.dir_lookup(parent)
            if look is None:
                unknown.append(parent)
            elif len(names) > PER_FILE_LIMIT:
                relist[look[0]] = (parent, look[1])
            else:
                perfile[look[0]] = (parent, look[1], names)
        for p in unknown:
            look = self.store.dir_lookup(p)      # e.g. a directory that moved earlier in this batch
            if look is not None:
                relist[look[0]] = (p, look[1])
                continue
            anc = self._known_ancestor(p)
            if anc is not None:
                relist[anc[0]] = (anc[1], anc[2])
        for did, (path, rid) in relist.items():
            self._relist(did, path, rid, walking, now)
        for did, (parent, rid, names) in perfile.items():
            if did not in relist and did not in self._relisted:
                self._update_names(did, parent, rid, names, walking, now)
        for p in rescans:
            self._rescan_path(p)
        self._commit()
        if moved_dsts and not walking:          # while walking, _move re-walks every moved dir
            for d in _outermost(moved_dsts):
                look = self.store.dir_lookup(d)
                if look is not None:
                    self._sub_scan(look[1], look[0], d)
        self._note_recent(noted, now)
        self.stats.batches += 1
        self.stats.batch_ops += self.stats.ops - n_before
        self.stats.flush_s += time.perf_counter() - t0

    def _note_recent(self, dirs: List[str], now: float) -> None:
        """Remember the folders this batch touched (see RECENT_EVENT_SECS)."""
        rd = self._recent_dirs
        for d in dirs:
            k = path_key(d)
            rd.pop(k, None)
            rd[k] = now                          # insertion order == age order
        cutoff = now - RECENT_EVENT_SECS
        while rd:
            k, t = next(iter(rd.items()))
            if t >= cutoff and len(rd) <= RECENT_EVENT_MAX:
                break
            del rd[k]

    def _moved_recent(self, src: str, dst: str) -> bool:
        """Folder ``src`` moved to ``dst``. True when an earlier batch applied events at or
        below ``src``: they may have been applied after the move had already happened on
        disk (the move's event comes later), so they found nothing at the old path (a new
        file not added, a modified one deleted, one moved in dropped) and the moved
        subtree has to be re-walked. The marks follow the folder to ``dst`` in case it
        moves again before its walk."""
        rd = self._recent_dirs
        if not rd:
            return False
        now = time.monotonic()
        sk = path_key(src)
        pre = sk if sk.endswith(os.sep) else sk + os.sep
        dk = path_key(dst)
        hit = False
        for k, t in list(rd.items()):
            if now - t > RECENT_EVENT_SECS:
                continue
            if k == sk:
                hit = True
                rd[dk] = t
            elif k.startswith(pre):
                hit = True
                rd[dk + os.sep + k[len(pre):]] = t
        return hit

    def _plan_relist(self, path: str, relist: Dict[int, tuple], unknown: List[str]) -> None:
        look = self.store.dir_lookup(path)
        if look is None:
            unknown.append(path)
        else:
            relist[look[0]] = (path, look[1])

    def _known_ancestor(self, path: str):
        """(dir_id, path, root_id) of the nearest indexed ancestor of ``path``."""
        p = path
        while True:
            parent = os.path.dirname(p)
            if parent == p or self._root_for_path(parent) is None:
                ri = self._root_for_path(path)
                return (ri.id, ri.path, ri.id) if ri is not None else None
            look = self.store.dir_lookup(parent)
            if look is not None:
                return look[0], parent, look[1]
            p = parent

    def _update_names(self, did: int, parent: str, rid: int, names, walking: bool, now: float) -> None:
        """Per-file update of a few names in one indexed dir (same rules as a walk)."""
        scan = self._flush_scan(rid)
        self._d("update_names", did, parent, sorted(names), walking)
        if scan is None:
            return
        ctx = scan.ctx
        olds = {}
        ents = []
        for name in names:
            o = self.store.entry_lookup(did, name)
            if o is not None:
                olds[name] = (o[0], name, o[1], o[2], o[3], o[4])
            ent = entry_for_path(ctx, parent, name, os.path.join(parent, name))
            if ent is not None:
                if ent[0] != name and ent[0] not in olds:      # iCloud stub resolved to its real name
                    o2 = self.store.entry_lookup(did, ent[0])
                    if o2 is not None:
                        olds[ent[0]] = (o2[0], ent[0], o2[1], o2[2], o2[3], o2[4])
                ents.append(ent)
        task = DirTask(did, parent, 1, False, (), scan)
        if walking:
            self._touched[did] = now
        kids = self._diff(task, ents, False, old=olds, one_level=True)
        if kids:
            scan.outstanding += len(kids)
            self.pool.push(kids)

    @staticmethod
    def _rebase_paths(paths: Dict[str, int], moves) -> Dict[str, int]:
        """Pending paths below a directory that moved in the same batch now live
        under the destination."""
        dir_moves = [(s, d) for s, d, is_dir in moves if is_dir]
        if not dir_moves:
            return paths
        out: Dict[str, int] = {}
        for p, bits in paths.items():
            for s, d in dir_moves:
                if p.startswith(s) and p[len(s):len(s) + 1] in ("\\", "/"):
                    p = d + p[len(s):]
            out[p] = out.get(p, 0) | bits
        return out

    def _relist(self, did: int, path: str, rid: int, walking: bool, now: float) -> None:
        done = self._relisted
        if did in done:
            return
        done.add(did)
        self._d("relist", did, path, walking)
        scan = self._flush_scan(rid)
        if scan is None:
            return
        task = DirTask(did, path, 1, False, (), scan)
        lst = list_dir(task, scan.ctx)
        if walking:
            self._touched[did] = now
        if lst.error is not None:
            if isinstance(lst.error, (FileNotFoundError, NotADirectoryError)):
                # the dir itself vanished: re-list its parent, which deletes it
                anc = self._known_ancestor(path)
                if anc is not None and anc[0] != did:
                    self._commit()
                    self._relist(anc[0], anc[1], anc[2], walking, now)
            return
        kids = self._diff(task, lst.entries, False, one_level=True)
        if kids:
            scan.outstanding += len(kids)
            self.pool.push(kids)

    def _rescan_path(self, path: str) -> None:
        ri = self._root_for_path(path)
        if ri is None:
            return
        if path_key(path) == path_key(ri.path):
            self._start_scan(ri.id, "full", background=False)
            return
        p = path
        while True:
            look = self.store.dir_lookup(p)
            if look is not None:
                self._sub_scan(look[1], look[0], p)
                return
            parent = os.path.dirname(p)
            if parent == p or self._root_for_path(parent) is None:
                self._start_scan(ri.id, "full", background=False)
                return
            p = parent

    def _move(self, src: str, dst: str, is_dir: bool, walking: bool, now: float,
              relist: Dict[int, tuple], unknown: List[str]) -> None:
        """REN (+DIRPATH) when both ends are indexed; otherwise delete and/or plan a
        re-list of the destination side (run after the moves are committed)."""
        O = self.core.ops
        store = self.store
        sp, sn = self._split(src)
        dp, dn = self._split(dst)
        sl = store.dir_lookup(sp)
        dl = store.dir_lookup(dp)
        old = store.entry_lookup(sl[0], sn) if sl is not None else None
        if old is None:
            # unknown source: the destination simply appeared
            if dl is not None:
                relist[dl[0]] = (dp, dl[1])
            else:
                unknown.append(dp)
            return
        oid, osz, omt, oct_, ofl = old
        if walking:
            self._touched[sl[0]] = now
        if dl is None or dl[1] != sl[1]:
            # destination parent unknown (or another root): delete + re-list the destination side
            self._ops.append((O.DEL, oid))
            if self._root_for_path(dst) is not None:
                if dl is not None:
                    relist[dl[0]] = (dp, dl[1])
                else:
                    unknown.append(dp)
            return
        scan = self._flush_scan(dl[1])
        ent = entry_for_path(scan.ctx, dp, dn, dst) if scan is not None else None
        if ent is None or ent[4] & NOSTAT:
            self._ops.append((O.DEL, oid))     # moved somewhere excluded, or gone again
            return
        if bool(ofl & FOLDER) != bool(ent[4] & FOLDER):
            self._ops.append((O.DEL, oid))
            relist[dl[0]] = (dp, dl[1])
            return
        name = ent[0]
        existing = store.entry_lookup(dl[0], name)
        if existing is not None and existing[0] != oid:
            self._ops.append((O.DEL, existing[0]))
        self._ops.append((O.REN, oid, dl[0], name))
        f = ent[4] & FLAG_MASK
        if ofl & FOLDER:
            self._ops.append((O.DIRPATH, oid, dst))
        if (osz, omt, oct_, ofl & ~UNREADABLE) != (ent[1], ent[2], ent[3], f):
            self._ops.append((O.UPD, oid, ent[1], ent[2], ent[3], f | (ofl & UNREADABLE)))
        if walking:
            self._touched[dl[0]] = now
            if ofl & FOLDER:
                # walk tasks queued under the old path are stale: re-walk the moved subtree
                self._commit()
                self._sub_scan(dl[1], oid, dst)

    # ------------------------------------------------------------------ power, housekeeping
    def _set_power(self, state, initial: bool = False) -> None:
        low = bool(state.low_power) and self.cfg.pause_on_battery
        changed = low != self.low_power
        self.low_power = low
        if self.pool is not None:
            self.pool.low_power = low
            self.pool.set_threads(1 if low else default_walker_threads())
        if changed and not initial:
            _log.info("power: %s", "low power (throttled)" if low else "normal")
            if not low:
                for ri in self._roots.values():
                    if ri.needs_full and ri.scan is None:
                        self._start_scan(ri.id, "full", background=True)
        self._status_dirty = True

    def _housekeeping(self, now: float) -> None:
        if self._scans_active():
            return
        if self._touched:
            self._touched.clear()
        minutes = self.cfg.periodic_rescan_minutes
        if not self.low_power and minutes and minutes > 0 and now - self.activity.last_search >= IDLE_FOR_PERIODIC:
            for rid in self._root_order:
                ri = self._roots[rid]
                period = min(minutes, LIMITED_RESCAN_MIN) if ri.live == "limited" else minutes
                if ri.last_scan_mono and now - ri.last_scan_mono >= period * 60:
                    ri.last_scan_mono = now
                    self._start_scan(rid, "full", background=True)
                    return
        if now - self._last_compact >= 60.0:
            self._last_compact = now
            try:
                if self.mem.compact_if_needed():
                    self._save_snapshot()
            except Exception:
                _log.debug("compaction failed", exc_info=True)

    # ------------------------------------------------------------------ status
    def _status_msg(self) -> P.IndexStatus:
        mem = self.mem
        counters = mem.counters() if mem is not None else None
        rows = []
        order = [self._roots[r] for r in self._root_order if r in self._roots]
        names = _display_names([ri.path for ri in order])
        any_scan = False
        for ri, name in zip(order, names):
            e, f, b = _root_counts(counters, ri.id) if counters is not None else (0, 0, 0)
            s = ri.scan
            scanned, rate = 0, 0.0
            if s is not None:
                any_scan = True
                scanned = s.entries
                dt = time.monotonic() - s.t0
                rate = scanned / dt if dt > 0 else 0.0
            rows.append(P.RootStatus(id=ri.id, path=ri.path, name=name, state=ri.state, entries=e, folders=f,
                                     bytes=b, denied_dirs=s.denied if s is not None else ri.denied,
                                     scanned_entries=scanned, expected_entries=ri.expected, rate=rate,
                                     live=ri.live, last_scan_end=ri.last_scan_end))
        live_rank = {"on": 0, "limited": 1, "off": 2}
        live = max((r.live for r in rows), key=lambda v: live_rank.get(v, 2), default="off")
        if not self.cfg.live_monitoring:
            live = "off"
        message = self._permission_wait_message()
        if not self._ready.is_set():
            state = "loading"
        elif any_scan:
            state = "scanning"
        elif self.low_power and any(ri.needs_full for ri in order):
            state = "paused"
            message = message or "Index paused (on battery)"
        else:
            state = "idle"
        return P.IndexStatus(state=state, roots=tuple(rows),
                             total_entries=_total(counters, "total_entries", "entries", "total") if counters else 0,
                             total_folders=_total(counters, "total_folders", "folders") if counters else 0,
                             live=live, index_generation=self._gen, message=message)

    def _permission_wait_message(self) -> str:
        """C.3 (TCC): a listing stuck > 2 s in a protected place means macOS is
        showing a permission prompt; say so (other walkers keep going)."""
        if self.pool is None:
            return ""
        slow = self.pool.slow_listings(2.0)
        if not slow:
            return ""
        self._status_dirty = True
        home = os.path.expanduser("~")
        for p in slow:
            name = protected_folder_name(p, home)
            if name:
                return f"Waiting for permission to read {name}…"
        return f"Waiting for {os.path.basename(slow[0].rstrip(os.sep)) or slow[0]}…"

    def _status_main(self) -> None:
        if not self.wait_ready():
            return
        while not self._stop.wait(STATUS_MIN_INTERVAL):
            now = time.monotonic()
            try:
                if (self._status_dirty and now - self._last_status_t >= STATUS_MIN_INTERVAL) or \
                        now - self._last_status_t >= STATUS_IDLE_INTERVAL:
                    self._status_dirty = False
                    self._last_status_t = now
                    self.send(self._status_msg())
                gen = self._gen
                if gen != self._sent_gen and now - self._last_changed_t >= CHANGED_MIN_INTERVAL:
                    self._sent_gen = gen
                    self._last_changed_t = now
                    self.send(P.IndexChanged(gen))
                if gen != self._refresh_gen and now - self._last_refresh_t >= REFRESH_MIN_INTERVAL:
                    self._refresh_gen = gen
                    self._last_refresh_t = now
                    self.search.request_refresh()
            except Exception:
                _log.debug("status tick failed", exc_info=True)


# ---------------------------------------------------------------------------- process entry

def _setup_logging(boot: P.EngineBootstrap) -> None:
    level = getattr(logging, (boot.log_level or "INFO").upper(), logging.INFO)
    try:
        from .. import applog
        applog.setup_logging("engine", Path(boot.log_dir))
        logging.getLogger().setLevel(level)
        return
    except Exception:
        pass
    try:
        from logging.handlers import RotatingFileHandler
        Path(boot.log_dir).mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(Path(boot.log_dir) / "engine.log", maxBytes=1_000_000,
                                                 backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(h)
        root.setLevel(level)
    except Exception:
        pass


def engine_main(conn, boot: P.EngineBootstrap) -> None:
    """``multiprocessing`` spawn target. Exits within 3 s of the GUI going away
    (EOF on the pipe, or a parent pid change on POSIX)."""
    # logging setup (~50 ms) runs alongside the engine's startup instead of before it
    log_thread = threading.Thread(target=_setup_logging, args=(boot,), name="ffs-logsetup", daemon=True)
    log_thread.start()
    parent = os.getppid()

    def send(msg):
        conn.send(msg)

    eng = Engine(boot, send)
    eng.start()
    log_thread.join(5.0)
    try:
        from .. import applog

        def on_exc(exc_type, exc, tb, thread_name):
            if thread_name in ("ffs-writer", "ffs-search", "MainThread"):
                eng._fatal(f"{exc_type.__name__}: {exc}", "".join(traceback.format_exception(exc_type, exc, tb)))
        applog.add_exception_listener(on_exc)
    except Exception:
        pass
    if os.name != "nt":
        def watch_parent():
            while not eng.dead.wait(5.0):
                if os.getppid() != parent:
                    _log.info("parent process gone; stopping")
                    eng.dead.set()
                    return
        threading.Thread(target=watch_parent, name="ffs-parent", daemon=True).start()
    try:
        while not eng.dead.is_set():
            if not conn.poll(0.5):
                continue
            msg = conn.recv()
            if isinstance(msg, P.Shutdown):
                break
            eng.handle(msg)
    except (EOFError, OSError):
        _log.info("GUI pipe closed; stopping")
    except Exception:
        _log.exception("engine rx loop failed")
    finally:
        killer = threading.Timer(3.0, os._exit, (0,))
        killer.daemon = True
        killer.start()
        eng.stop(timeout=2.5)
        try:
            conn.close()
        except Exception:
            pass
        logging.shutdown()
    os._exit(0)
