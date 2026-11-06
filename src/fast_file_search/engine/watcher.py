"""Live monitoring (design D): one watchdog observer, one recursive watch per
root, a cheap per-event filter and a coalescing path dict flushed by a single
timer thread. No Qt, no sqlite3.

Per event (watchdog thread): decode, map to the configured root spelling
(aliases such as macOS /private/var), drop own files, drop excluded
components below the root, drop directory "modified" noise (except on macOS),
then ``pending[path] |= bits``. The flush thread hands a ``Batch`` to the
engine after ``quiet`` seconds without events or ``max_wait`` after the first
one. More than ``max_pending`` paths, or an OS overflow, collapses the batch
into recursive rescans of at most 16 common ancestors (else the root).
"""
from __future__ import annotations

import errno
import logging
import os
import sys
import threading
import time
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from .exclusions import PACKAGE_SUFFIXES, Exclusions, dedupe_roots, path_key, sep_of

_log = logging.getLogger(__name__)

FILE, DIR, RECURSIVE = 1, 2, 4
MAX_COLLAPSE_DIRS = 16
# inotify (Linux) runs out of watches/instances on big trees: degrade that root.
LIMIT_ERRNOS = {errno.ENOSPC, errno.EMFILE, errno.ENFILE}


class Batch(NamedTuple):
    paths: Dict[str, int]                  # path (root spelling) -> FILE|DIR|RECURSIVE bits
    moves: List[Tuple[str, str, bool]]     # (src, dst, is_dir), both inside roots
    collapsed: bool                        # flood/overflow: paths are RECURSIVE rescans only


def close_leaked_inotify(exc: BaseException) -> None:
    """watchdog's Inotify.__init__ does not close its inotify fd when adding
    watches fails part-way, so the watches already added keep eating the
    per-user limit until we exit. Close it."""
    tb = exc.__traceback__
    while tb is not None:
        obj = tb.tb_frame.f_locals.get("self")
        if type(obj).__name__ == "Inotify" and not getattr(obj, "_closed", True):
            for name in ("_inotify_fd", "_kill_r", "_kill_w"):
                fd = getattr(obj, name, None)
                if isinstance(fd, int) and fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            obj._closed = True
        tb = tb.tb_next


class Coalescer:
    """Thread-safe pending-path dict with a single flush thread."""

    def __init__(self, on_batch: Callable[[Batch], None], *, quiet: float = 0.5, max_wait: float = 2.0,
                 max_pending: int = 10000, collapse: Optional[Callable[[Iterable[str]], List[str]]] = None):
        self._on_batch = on_batch
        self.quiet = quiet
        self.max_wait = max_wait
        self.max_pending = max_pending
        self._collapse = collapse or (lambda dirs: sorted(set(dirs))[:MAX_COLLAPSE_DIRS])
        self._cv = threading.Condition(threading.Lock())
        self._paths: Dict[str, int] = {}
        self._moves: List[Tuple[str, str, bool]] = []
        self._flood_dirs: set = set()
        self._flood = False
        self._t_first: Optional[float] = None
        self._t_last = 0.0
        self._stop = False
        self._paused = False
        self.batches = 0
        self._thread = threading.Thread(target=self._run, name="ffs-watch-flush", daemon=True)
        self._thread.start()

    # -------------------------------------------------------------- producers
    def add(self, path: str, bits: int) -> None:
        with self._cv:
            now = time.monotonic()
            if self._flood:
                self._flood_dirs.add(os.path.dirname(path) if not bits & (DIR | RECURSIVE) else path)
            else:
                paths = self._paths
                paths[path] = paths.get(path, 0) | bits
                if len(paths) > self.max_pending:
                    self._go_flood()
            self._t_last = now
            if self._t_first is None:
                self._t_first = now
                self._cv.notify()

    def add_move(self, src: str, dst: str, is_dir: bool) -> None:
        with self._cv:
            now = time.monotonic()
            if self._flood:
                self._flood_dirs.add(os.path.dirname(src))
                self._flood_dirs.add(os.path.dirname(dst) if not is_dir else dst)
            else:
                self._moves.append((src, dst, is_dir))
            self._t_last = now
            if self._t_first is None:
                self._t_first = now
                self._cv.notify()

    def overflow(self, path: str) -> None:
        """The OS dropped events below ``path``: rescan it."""
        with self._cv:
            self._go_flood()
            self._flood_dirs.add(path)
            now = time.monotonic()
            self._t_last = now
            if self._t_first is None:
                self._t_first = now
                self._cv.notify()

    def _go_flood(self) -> None:
        if self._flood:
            return
        self._flood = True
        fd = self._flood_dirs
        for p, bits in self._paths.items():
            fd.add(p if bits & (DIR | RECURSIVE) else os.path.dirname(p))
        for s, d, is_dir in self._moves:
            fd.add(os.path.dirname(s))
            fd.add(d if is_dir else os.path.dirname(d))
        self._paths = {}
        self._moves = []

    @property
    def pending(self) -> int:
        with self._cv:
            return len(self._paths) + len(self._moves) + len(self._flood_dirs)

    # -------------------------------------------------------------- flush
    def _take(self) -> Optional[Batch]:
        if self._flood:
            dirs = self._collapse(self._flood_dirs)
            b = Batch({d: RECURSIVE for d in dirs}, [], True)
        elif self._paths or self._moves:
            b = Batch(self._paths, self._moves, False)
        else:
            b = None
        self._paths, self._moves, self._flood_dirs, self._flood = {}, [], set(), False
        self._t_first = None
        return b

    def flush_now(self) -> Optional[Batch]:
        with self._cv:
            b = self._take()
        if b is not None:
            self.batches += 1
            self._on_batch(b)
        return b

    def _run(self) -> None:
        cv = self._cv
        while True:
            with cv:
                while not self._stop and self._t_first is None:
                    cv.wait()
                if self._stop:
                    return
                now = time.monotonic()
                due = min(self._t_last + self.quiet, self._t_first + self.max_wait)
                if now < due:
                    cv.wait(due - now)
                    continue
                b = self._take()
            if b is not None:
                self.batches += 1
                try:
                    self._on_batch(b)
                except Exception:
                    _log.exception("watch batch handler failed")

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._paths, self._moves, self._flood_dirs = {}, [], set()
            self._cv.notify_all()
        self._thread.join(2.0)


class _Root(NamedTuple):
    spelling: str          # configured root path
    key: str               # path key of the event-side spelling (the root or an alias)
    key_sep: str           # key with a trailing separator
    alias_len: int         # length of the event-side prefix to replace by `spelling`
    skey_sep: str          # path key of `spelling` with a trailing separator
    below_prefixes: tuple  # absolute excludes strictly below this root (with sep)
    below_exact: frozenset


class Watcher:
    """watchdog observer + filter + coalescer for a set of roots."""

    def __init__(self, ex: Exclusions, on_batch: Callable[[Batch], None], *, quiet: float = 0.5,
                 max_wait: float = 2.0, max_pending: int = 10000,
                 observer_factory: Optional[Callable[[], object]] = None,
                 on_error: Optional[Callable[[str, BaseException], None]] = None):
        self.ex = ex
        self.platform = ex.platform
        self.sep = sep_of(ex.platform)
        self._keep_dir_modified = ex.platform == "darwin"   # FSEvents may report only the dir
        self._observer_factory = observer_factory
        self._on_error = on_error
        self.coalescer = Coalescer(on_batch, quiet=quiet, max_wait=max_wait, max_pending=max_pending,
                                   collapse=self.collapse)
        self._roots: List[_Root] = []
        self._paths: List[str] = []
        self._observer = None
        self._lock = threading.RLock()
        self.live: Dict[str, str] = {}       # root -> 'on' | 'limited' | 'off'
        self._limit_logged = False
        self.events_seen = 0
        self.events_kept = 0

    # -------------------------------------------------------------- roots
    def set_roots(self, roots: Iterable[str], check_exists: bool = True) -> List[str]:
        """Replace the watched set (missing folders skipped, duplicates and nested
        roots dropped). Returns the kept roots."""
        kept = dedupe_roots([r for r in roots if r and (not check_exists or os.path.isdir(r))], self.platform)
        entries: List[_Root] = []
        for r in kept:
            entries.append(self._root_entry(r, r))
            try:
                real = os.path.realpath(r) if check_exists else r
            except (OSError, ValueError):
                real = r
            if path_key(real, self.platform) != path_key(r, self.platform):
                entries.append(self._root_entry(real, r))
        # longest key first so a nested alias wins
        entries.sort(key=lambda e: -len(e.key))
        with self._lock:
            self._roots = entries
            self._paths = kept
        return kept

    def _root_entry(self, event_side: str, root: str) -> _Root:
        pre, exact = self.ex.below_root(root)
        k = path_key(event_side, self.platform)
        sk = path_key(root, self.platform)
        sep = self.sep
        return _Root(root, k, k if k.endswith(sep) else k + sep, len(event_side),
                     sk if sk.endswith(sep) else sk + sep, pre, exact)

    def add_alias(self, alias: str, root: str) -> None:
        """Another spelling of ``root`` that events may use (tests, macOS /private)."""
        with self._lock:
            self._roots = sorted(self._roots + [self._root_entry(alias, root)], key=lambda e: -len(e.key))

    @property
    def roots(self) -> List[str]:
        return list(self._paths)

    def map_path(self, path: str) -> Optional[Tuple[str, _Root]]:
        """``(path in root spelling, root)`` or None when outside every root."""
        k = self.ex.pkey(path)
        for r in self._roots:
            if k.startswith(r.key_sep):
                tail = path[r.alias_len:].lstrip("\\/") if self.platform == "win32" else path[r.alias_len:].lstrip("/")
                sp = r.spelling
                if not tail:
                    return sp, r
                return (sp + tail if sp.endswith(self.sep) else sp + self.sep + tail), r
            if k == r.key:
                return r.spelling, r
        return None

    # -------------------------------------------------------------- filter
    def accept(self, path: str) -> Optional[str]:
        """The event filter: the path in root spelling, or None if dropped."""
        ex = self.ex
        m = self.map_path(path)
        if m is None:
            return None
        p, r = m
        k = ex.pkey(p)
        if ex.own_prefixes and (k in ex.own_dir_keys or k.startswith(ex.own_prefixes)):
            return None
        if p is r.spelling:
            return p                     # the root itself
        bs = r.skey_sep
        rel = k[len(bs):] if k.startswith(bs) else k
        parts = rel.split(self.sep)
        if not ex.names.isdisjoint(parts):
            return None
        if ex._glob_rx is not None:
            g = ex._glob_rx
            for x in parts:
                if g(x):
                    return None
        if ex.trash_prefix:
            for x in parts:
                if x.startswith(".trash-"):
                    return None
        if r.below_exact and (k in r.below_exact or k.startswith(r.below_prefixes)):
            return None
        if ex.packages_apply and not ex.descend_packages:
            for x in parts[:-1]:
                if x.endswith(PACKAGE_SUFFIXES):
                    return None
        if not ex.index_hidden:
            for x in parts:
                if x[:1] == ".":
                    return None
        if ex.has_sequences:
            dirp, _, name = p.rpartition(self.sep)
            if ex.seq_hit(name, dirp, r.spelling):
                return None
            # an ancestor component may end a sequence too
            acc = r.spelling
            for raw in p[len(r.spelling):].strip("\\/").split(self.sep)[:-1]:
                if ex.seq_hit(raw, acc, r.spelling):
                    return None
                acc = acc + self.sep + raw if not acc.endswith(self.sep) else acc + raw
        return p

    def feed(self, kind: str, src, is_dir: bool = False, dest=None, synthetic: bool = False) -> bool:
        """One watchdog event. ``kind`` is watchdog's event_type. Returns True if kept."""
        self.events_seen += 1
        if synthetic:
            return False   # sub-events of a moved/created dir: covered by REN+DIRPATH / the new-dir scan
        if kind in ("opened", "closed", "closed_no_write"):
            return False
        if not isinstance(src, str):
            src = os.fsdecode(src)
        if kind == "moved":
            if dest is not None and not isinstance(dest, str):
                dest = os.fsdecode(dest)
            s = self.accept(src)
            d = self.accept(dest) if dest else None
            if s is not None and d is not None:
                self.coalescer.add_move(s, d, is_dir)
            elif d is not None:
                self.coalescer.add(d, FILE)
            elif s is not None:
                self.coalescer.add(s, FILE)
            else:
                return False
            self.events_kept += 1
            return True
        if kind == "modified" and is_dir:
            if not self._keep_dir_modified:
                return False
            p = self.accept(src)
            if p is None:
                return False
            self.coalescer.add(p, DIR)
            self.events_kept += 1
            return True
        p = self.accept(src)
        if p is None:
            return False
        self.coalescer.add(p, FILE)
        self.events_kept += 1
        return True

    def collapse(self, dirs: Iterable[str]) -> List[str]:
        """Nearest common ancestors (≤ 16 overall, else the roots), in root spelling."""
        by_root: Dict[str, set] = {}
        for d in dirs:
            m = self.map_path(d)
            if m is None:
                continue
            by_root.setdefault(m[1].spelling, set()).add(m[0])
        out: List[str] = []
        for root, ds in by_root.items():
            ds = _drop_nested(ds, self.platform)
            while len(ds) > MAX_COLLAPSE_DIRS:
                nxt = set()
                for d in ds:
                    if path_key(d, self.platform) == path_key(root, self.platform):
                        nxt.add(d)
                    else:
                        parent = os.path.dirname(d) if self.platform == sys.platform else d.rsplit(self.sep, 1)[0]
                        nxt.add(parent if len(parent) >= len(root) else root)
                if nxt == ds:
                    break
                ds = _drop_nested(nxt, self.platform)
            out.extend(sorted(ds))
        if len(out) > MAX_COLLAPSE_DIRS:
            return sorted(by_root)
        return out

    # -------------------------------------------------------------- lifecycle
    def _make_observer(self):
        if self._observer_factory is not None:
            return self._observer_factory()
        from watchdog.observers import Observer
        obs = Observer()
        ec = getattr(obs, "_emitter_class", None)
        win = _windows_emitter(self, ec) if ec is not None and sys.platform == "win32" else None
        if win is not None:
            obs._emitter_class = win
            obs._ffs_feeding = True
        elif ec is not None:
            watcher = self

            class _FeedingEmitter(ec):  # type: ignore[misc, valid-type]
                """Feeds the filter directly instead of the observer queue."""

                def queue_event(self, event):  # noqa: N802 (watchdog API)
                    try:
                        watcher.feed(event.event_type, event.src_path, event.is_directory,
                                     getattr(event, "dest_path", None) or None,
                                     getattr(event, "is_synthetic", False))
                    except Exception:
                        _log.debug("watch event dropped", exc_info=True)

            obs._emitter_class = _FeedingEmitter
            obs._ffs_feeding = True
        return obs

    def start(self) -> Dict[str, str]:
        """Schedule every root. Returns root -> live state."""
        with self._lock:
            self._stop_observer()
            live: Dict[str, str] = {}
            if not self._paths:
                self.live = live
                return live
            try:
                obs = self._make_observer()
                if hasattr(obs, "daemon"):
                    obs.daemon = True
                obs.start()
            except Exception as exc:
                self._report("", exc)
                self.live = {r: "off" for r in self._paths}
                return self.live
            handler = None
            if not getattr(obs, "_ffs_feeding", False):
                handler = _make_handler(self)
            for root in sorted(self._paths):
                try:
                    obs.schedule(handler, root, recursive=True)
                    live[root] = "on"
                except OSError as exc:
                    if exc.errno in LIMIT_ERRNOS:
                        close_leaked_inotify(exc)
                        self._report_limit(root, exc)
                        live[root] = "limited"
                    else:
                        self._report(root, exc)
                        live[root] = "off"
                except Exception as exc:
                    self._report(root, exc)
                    live[root] = "off"
            if not any(v == "on" for v in live.values()):
                try:
                    obs.stop()
                except Exception:
                    pass
            else:
                self._observer = obs
            self.live = live
            return live

    def _report_limit(self, root: str, exc: OSError) -> None:
        if not self._limit_logged:
            self._limit_logged = True
            _log.warning("Live file watching limited for %s (%s). The index is still kept up to date by "
                         "periodic rescans. On Linux raise fs.inotify.max_user_watches / "
                         "max_user_instances to enable it.", root, exc)
            self._report(root, exc)

    def _report(self, root: str, exc: BaseException) -> None:
        if self._on_error is not None:
            try:
                self._on_error(root, exc)
            except Exception:
                pass

    def _stop_observer(self) -> None:
        obs, self._observer = self._observer, None
        if obs is not None:
            try:
                obs.stop()
                if obs.is_alive():
                    obs.join(timeout=3)
            except Exception:
                pass

    def stop(self) -> None:
        with self._lock:
            self._stop_observer()
        self.coalescer.stop()

    @property
    def is_running(self) -> bool:
        return self._observer is not None


def _windows_emitter(w: "Watcher", base):
    """watchdog 6.x ReadDirectoryChangesW emitter that filters the raw path first
    (no os.path.isdir or event objects for excluded/own paths), skips watchdog's
    synthetic sub-event walks (the engine re-lists new/moved dirs itself) and
    reports buffer overflows (0 bytes returned while running) as a rescan.
    Returns None when the installed watchdog does not have the expected internals."""
    try:
        import inspect

        from watchdog.observers import winapi
        if not (hasattr(base, "_read_events") and hasattr(winapi, "ReadDirectoryChangesW")
                and hasattr(winapi, "_parse_event_buffer") and hasattr(winapi, "WATCHDOG_FILE_NOTIFY_FLAGS")
                and len(inspect.signature(winapi._parse_event_buffer).parameters) == 2):
            return None
        import ctypes
        from ctypes import wintypes
        rdcw = winapi.ReadDirectoryChangesW
        flags = winapi.WATCHDOG_FILE_NOTIFY_FLAGS
        aborted = winapi.ERROR_OPERATION_ABORTED
        bufsize = getattr(winapi, "BUFFER_SIZE", 64000)
        parse = winapi._parse_event_buffer
        A_ADD, A_DEL, A_MOD = winapi.FILE_ACTION_CREATED, winapi.FILE_ACTION_DELETED, winapi.FILE_ACTION_MODIFIED
        A_OLD, A_NEW = winapi.FILE_ACTION_RENAMED_OLD_NAME, winapi.FILE_ACTION_RENAMED_NEW_NAME
        A_SELF = getattr(winapi, "FILE_ACTION_REMOVED_SELF", 0xFFFE)
    except Exception:
        return None
    isdir = os.path.isdir

    class _WinEmitter(base):  # type: ignore[misc, valid-type]
        _ffs_old = ""

        def queue_events(self, timeout):  # noqa: D401 (watchdog API)
            handle = getattr(self, "_whandle", None)
            if not handle:
                return
            root = self.watch.path
            buf = getattr(self, "_ffs_buf", None)
            if buf is None:
                # one buffer per watch (watchdog allocates and copies 64 KB per call)
                buf = self._ffs_buf = ctypes.create_string_buffer(bufsize)
                self._ffs_n = wintypes.DWORD()
            n = self._ffs_n
            try:
                rdcw(handle, ctypes.byref(buf), len(buf), self.watch.is_recursive, flags, ctypes.byref(n),
                     None, None)
            except OSError as exc:
                if getattr(exc, "winerror", None) == aborted or self.stopped_event.is_set():
                    return
                if not os.path.isdir(root):
                    w.feed("deleted", root, True)
                    self.stop()
                    return
                w.coalescer.overflow(root)
                return
            nbytes = n.value
            if nbytes == 0:
                if not self.stopped_event.is_set():
                    _log.info("watch buffer overflow on %s: rescanning", root)
                    w.coalescer.overflow(root)
                return
            events = _parse_fni(ctypes.string_at(buf, nbytes))
            if len(events) < 64:
                # let the kernel batch the next events (a 64 KB buffer holds ~1000)
                self.stopped_event.wait(0.02)
            base_path = root if root.endswith("\\") else root + "\\"
            accept, feed = w.accept, w.feed
            for action, rel in events:
                p = base_path + rel
                w.events_seen += 1
                try:
                    if action == A_OLD:
                        self._ffs_old = p
                    elif action == A_NEW:
                        old, self._ffs_old = self._ffs_old, ""
                        if accept(p) is None and (not old or accept(old) is None):
                            continue
                        w.events_seen -= 1
                        feed("moved", old, isdir(p), p)
                    elif action == A_MOD:
                        if accept(p) is None or isdir(p):
                            continue           # excluded, or dir-modified noise
                        w.events_seen -= 1
                        feed("modified", p, False)
                    elif action in (A_ADD, A_DEL):
                        w.events_seen -= 1
                        feed("created" if action == A_ADD else "deleted", p, False)
                    elif action == A_SELF:
                        w.events_seen -= 1
                        feed("deleted", root, True)
                        self.stop()
                except Exception:
                    _log.debug("watch event dropped", exc_info=True)

    return _WinEmitter


def _parse_fni(data: bytes) -> list:
    """FILE_NOTIFY_INFORMATION records -> [(action, relative name)], by offset
    (watchdog re-slices the buffer per record, which is quadratic)."""
    import struct
    unpack = struct.unpack_from
    out = []
    off = 0
    end = len(data)
    while off + 12 <= end:
        nxt, action, nlen = unpack("<III", data, off)
        out.append((action, data[off + 12:off + 12 + nlen].decode("utf-16-le", "surrogatepass")))
        if nxt <= 0:
            break
        off += nxt
    return out


def _drop_nested(ds: set, platform: str) -> set:
    keyed = sorted((path_key(d, platform), d) for d in ds)
    sep = sep_of(platform)
    out = []
    last = None
    for k, d in keyed:
        if last is not None and (k == last or k.startswith(last if last.endswith(sep) else last + sep)):
            continue
        out.append(d)
        last = k
    return set(out)


def _make_handler(w: Watcher):
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            w.feed(event.event_type, event.src_path, event.is_directory,
                   getattr(event, "dest_path", None) or None, getattr(event, "is_synthetic", False))

    return _Handler()
