"""Directory walker (design C.1): ``os.scandir`` listings for the writer thread.

A walker thread turns a ``DirTask`` into a ``Listing``: one tuple per kept entry
``(name, size, mtime, ctime, flags, path_or_None)``. ``flags`` carries the
protocol bits plus the internal ``DESCEND`` bit (stripped by the writer) that
says whether the writer should schedule a child task for a folder.

Descent rules: no symlinks; no Windows junctions / mount points (reparse tag
0xA0000003); POSIX: stay on the root's device set (macOS firmlink pair
included) and never enter a (st_dev, st_ino) twice; macOS packages are leaves
unless configured otherwise; walk-time absolute excludes and the engine's own
dirs are dropped. No pathlib and no per-entry Python calls beyond the loop.
"""
from __future__ import annotations

import ntpath
import os
import stat as _stat
import sys
import threading
import time
from typing import Callable, List, NamedTuple, Optional

from .exclusions import PACKAGE_SUFFIXES, Exclusions
from .protocol import DATALESS, FOLDER, HIDDEN, MOUNT, PACKAGE, SYMLINK, SYSTEM

DESCEND = 1 << 16          # internal: schedule a child task for this folder
NOSTAT = 1 << 17           # internal: entry exists but could not be stat'ed (keep what we had)
FLAG_MASK = 0xFFFF

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
# OneDrive / cloud placeholders whose data is not on disk.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_WIN_DATALESS = _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
# macOS st_flags (stat.UF_HIDDEN / stat.SF_DATALESS on 3.13; literals as fallback).
UF_HIDDEN = getattr(_stat, "UF_HIDDEN", 0x8000)
SF_DATALESS = getattr(_stat, "SF_DATALESS", 0x40000000)

_S_ISDIR = _stat.S_ISDIR
_S_ISLNK = _stat.S_ISLNK


class DirTask(NamedTuple):
    dir_id: int
    path: str
    depth: int
    is_new: bool              # dir has no stored children: every entry is an INS, no query
    entry: tuple              # the folder's own (size, mtime, ctime, flags) from its parent; () for a root
    scan: object              # the owning scan (service._Scan): root id, mode, context, counters


class Listing(NamedTuple):
    task: DirTask
    entries: Optional[list]
    error: Optional[OSError]
    t_listed: float           # time.monotonic() when the listing started


def same_volume_devices(root: str, platform: Optional[str] = None) -> set:
    """Device ids the walk may enter without crossing a mount point (POSIX).
    macOS 10.15+: "/" (sealed system volume) and /System/Volumes/Data are one
    logical volume through firmlinks."""
    p = sys.platform if platform is None else platform
    if p == "win32":
        return set()
    try:
        devs = {os.stat(root).st_dev}
    except OSError:
        return set()
    if p == "darwin":
        try:
            pair = {os.stat("/").st_dev, os.stat("/System/Volumes/Data").st_dev}
            if devs & pair:
                devs |= pair
        except OSError:
            pass
    return devs


class WalkContext:
    """Per-scan walking state (shared by the walker threads of that scan)."""

    def __init__(self, ex: Exclusions, *, devices: Optional[set] = None, scandir: Callable = os.scandir,
                 root: str = ""):
        self.ex = ex
        self.platform = ex.platform
        self.win = ex.platform == "win32"
        self.darwin = ex.platform == "darwin"
        self.st_flags = self.darwin or ex.platform.startswith(("freebsd", "openbsd", "netbsd"))
        self.birth = self.platform in ("win32", "darwin")
        self.devices = devices if devices is not None else set()
        self.visited: set = set()
        self.scandir = scandir
        self.root = root
        if not self.win and root:
            try:
                st = os.stat(root)
                self.visited.add((st.st_dev, st.st_ino))
            except OSError:
                pass


def _is_drive_root(path: str) -> bool:
    drive, rest = ntpath.splitdrive(path)
    return bool(drive) and rest.strip("\\/") == ""


def scan_entries(ctx: WalkContext, dir_path: str, it) -> list:
    """Turn an iterable of ``os.DirEntry``-like objects into listing tuples."""
    ex = ctx.ex
    names = ex.names
    fold = ex.fold
    darwin = ctx.darwin
    win = ctx.win
    use_st_flags = ctx.st_flags
    birth = ctx.birth
    glob = ex._glob_rx
    trash = ex.trash_prefix
    seq = ex.has_sequences
    seq_last = ex._seq_last
    keyf = ex.key
    index_hidden = ex.index_hidden
    packages = ex.packages_apply
    descend_packages = ex.descend_packages
    devices = ctx.devices
    visited = ctx.visited
    abs_dirs = ex.abs_dirs
    own_dirs = ex.own_dir_keys
    pkey = ex.pkey
    own_check = ex.near_own(dir_path)
    drive_root = win and bool(ex.drive_names) and _is_drive_root(dir_path)
    out: list = []
    append = out.append
    stubs = False
    lens = ex.name_lens
    fold_all = fold and glob is not None
    for e in it:
        name = e.name
        if not fold:
            k = name
            if k in names:
                continue
        elif fold_all or len(name) in lens or not name.isascii():
            # folding never changes the length of an ASCII name: skip it unless it could match
            k = name.lower() if name.isascii() else keyf(name)
            if k in names:
                continue
        else:
            k = None
        if glob is not None and glob(k):
            continue
        if trash and name.startswith(".Trash-"):
            continue
        if seq and (k if k is not None else name.lower()) in seq_last and ex.seq_hit(name, dir_path, ctx.root or None):
            continue
        if own_check and ex.is_own(e.path):
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            append((name, 0, 0, 0, NOSTAT, None))
            continue
        mode = st.st_mode
        if _S_ISDIR(mode):
            flags = FOLDER
        elif _S_ISLNK(mode):
            flags = SYMLINK
        else:
            flags = 0
        if name[:1] == ".":
            flags |= HIDDEN
        if win:
            a = st.st_file_attributes
            if a & _FILE_ATTRIBUTE_HIDDEN:
                flags |= HIDDEN
            if a & _FILE_ATTRIBUTE_SYSTEM:
                flags |= SYSTEM
            if a & _WIN_DATALESS:
                flags |= DATALESS
        elif use_st_flags:
            sf = getattr(st, "st_flags", 0)
            if sf & UF_HIDDEN:
                flags |= HIDDEN
            if sf & SF_DATALESS:
                flags |= DATALESS
        if darwin and flags & HIDDEN and name.endswith(".icloud") and len(name) > 8 and not flags & FOLDER:
            # legacy iCloud stub ".Name.ext.icloud": index under the real name
            name = name[1:-7]
            flags = (flags & ~HIDDEN) | DATALESS | (HIDDEN if name[:1] == "." else 0)
            stubs = True
        if not index_hidden and flags & HIDDEN:
            continue
        mtime = int(st.st_mtime)
        if birth:
            try:
                ctime = int(st.st_birthtime)
            except AttributeError:
                ctime = 0
        else:
            ctime = 0
        if flags & FOLDER:
            path = e.path
            pk = pkey(path)
            if pk in abs_dirs or pk in own_dirs:
                continue
            if k is None:
                k = name.lower()
            if drive_root and k in ex.drive_names:
                continue
            descend = True
            if win:
                if st.st_reparse_tag == IO_REPARSE_TAG_MOUNT_POINT:
                    flags |= MOUNT
                    descend = False
            else:
                dev = st.st_dev
                if devices and dev not in devices:
                    flags |= MOUNT
                    descend = False
                else:
                    ino = st.st_ino
                    if ino:
                        vk = (dev, ino)
                        if vk in visited:
                            descend = False   # bind-mount loop / already walked
                        else:
                            visited.add(vk)
            if packages and k.endswith(PACKAGE_SUFFIXES):
                flags |= PACKAGE
                if not descend_packages:
                    descend = False
            if descend:
                flags |= DESCEND
            append((name, 0, mtime, ctime, flags, path))
        else:
            append((name, st.st_size, mtime, ctime, flags, None))
    if stubs:
        seen = {}
        for i, t in enumerate(out):
            if t[0] in seen:
                # a real file and its stub: keep the real one
                j = seen[t[0]]
                drop = i if t[4] & DATALESS else j
                out[drop] = None
            else:
                seen[t[0]] = i
        out = [t for t in out if t is not None]
    return out


def list_dir(task: DirTask, ctx: WalkContext) -> Listing:
    t = time.monotonic()
    try:
        with ctx.scandir(task.path) as it:
            entries = scan_entries(ctx, task.path, it)
    except OSError as exc:
        return Listing(task, None, exc, t)
    return Listing(task, entries, None, t)


class _StatEntry:
    """A DirEntry look-alike for one path (watcher single-file updates)."""

    __slots__ = ("name", "path", "_st")

    def __init__(self, name: str, path: str, st):
        self.name, self.path, self._st = name, path, st

    def stat(self, follow_symlinks: bool = False):
        return self._st

    def is_junction(self) -> bool:
        return getattr(self._st, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT


def entry_for_path(ctx: WalkContext, dir_path: str, name: str, path: str):
    """Listing tuple for a single path under the same rules as a walk, or None
    if the path is missing or excluded. Raises nothing."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    out = scan_entries(ctx, dir_path, (_StatEntry(name, path, st),))
    return out[0] if out else None


class Activity:
    """Shared between the rx thread (searches) and walker/writer threads."""

    def __init__(self):
        self.pause_until = 0.0
        self.last_search = 0.0

    def poke(self, seconds: float = 0.15) -> None:
        """A search request arrived: background work pauses until it is answered
        (``seconds`` is only a safety cap)."""
        now = time.monotonic()
        self.last_search = now
        self.pause_until = now + seconds

    def release(self, grace: float = 0.003) -> None:
        """The pending search was answered: resume shortly (never starve indexing)."""
        self.pause_until = min(self.pause_until, time.monotonic() + grace)

    def recently_active(self, window: float = 2.0) -> bool:
        return time.monotonic() - self.last_search < window

    def wait(self, stop: Optional[threading.Event] = None) -> None:
        while True:
            d = self.pause_until - time.monotonic()
            if d <= 0 or (stop is not None and stop.is_set()):
                return
            time.sleep(min(d, 0.05))


class WalkerPool:
    """Walker threads pulling ``DirTask`` from one LIFO (depth-first), with at
    most ``max_inflight`` listings not yet consumed by the writer."""

    def __init__(self, deliver: Callable[[Listing], None], activity: Activity, nthreads: int = 1,
                 max_inflight: int = 8):
        self._deliver = deliver
        self._activity = activity
        self._cv = threading.Condition()
        self._stack: List[DirTask] = []
        self._inflight = 0
        self._max_inflight = max_inflight
        self._active = max(1, nthreads)
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self.low_power = False
        self._current: dict = {}          # walker index -> (path, t_start) of the listing in progress
        self._ensure_threads(self._active)

    def _ensure_threads(self, n: int) -> None:
        while len(self._threads) < n:
            i = len(self._threads)
            t = threading.Thread(target=self._run, args=(i,), name=f"ffs-walker-{i}", daemon=True)
            self._threads.append(t)
            t.start()

    def set_threads(self, n: int) -> None:
        n = max(1, n)
        with self._cv:
            self._active = n
            self._ensure_threads(n)
            self._cv.notify_all()

    @property
    def threads(self) -> int:
        return self._active

    def push(self, tasks) -> None:
        if not tasks:
            return
        with self._cv:
            # reversed so the first child is walked first (LIFO)
            self._stack.extend(reversed(tasks))
            self._cv.notify_all()

    def done(self) -> None:
        """The writer consumed one listing."""
        with self._cv:
            self._inflight -= 1
            self._cv.notify()

    def remove(self, pred: Callable[[DirTask], bool]) -> List[DirTask]:
        with self._cv:
            keep, gone = [], []
            for t in self._stack:
                (gone if pred(t) else keep).append(t)
            self._stack = keep
            return gone

    def slow_listings(self, seconds: float = 2.0) -> List[str]:
        """Paths whose listing has been running for more than ``seconds`` (a macOS
        privacy prompt blocks scandir on protected folders)."""
        now = time.monotonic()
        return [p for p, t in list(self._current.values()) if now - t > seconds]

    def idle(self) -> bool:
        with self._cv:
            return not self._stack and self._inflight == 0

    def pending(self) -> int:
        with self._cv:
            return len(self._stack) + self._inflight

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._stack.clear()
            self._cv.notify_all()

    def join(self, timeout: float = 1.0) -> None:
        end = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, end - time.monotonic()))

    def _run(self, idx: int) -> None:
        cv = self._cv
        while True:
            with cv:
                while not self._stop.is_set() and (
                        not self._stack or self._inflight >= self._max_inflight or idx >= self._active):
                    cv.wait(0.5)
                if self._stop.is_set():
                    return
                task = self._stack.pop()
                self._inflight += 1
            try:
                self._activity.wait(self._stop)
                scan = task.scan
                if self.low_power and getattr(scan, "background", False):
                    time.sleep(0.005)
                self._current[idx] = (task.path, time.monotonic())
                try:
                    lst = list_dir(task, scan.ctx)
                finally:
                    self._current.pop(idx, None)
            except BaseException as exc:  # never lose the in-flight slot
                lst = Listing(task, None, OSError(str(exc)), time.monotonic())
            self._deliver(lst)
