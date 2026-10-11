from __future__ import annotations

import errno
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Set, Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import AppConfig
from .indexer import is_system_name
from .persistent_index import PersistentIndex
from .utils.file_utils import ExcludeMatcher, build_file_info, split_path
from .utils.platform_utils import dedupe_roots, is_path_under

_TREE_BATCH = 1000
_log = logging.getLogger(__name__)

# inotify (Linux) runs out of watches/instances on big trees: the watcher then
# degrades to "no live updates" for that root; periodic rescans keep it fresh.
_LIMIT_ERRNOS = {errno.ENOSPC, errno.EMFILE, errno.ENFILE}


def _close_leaked_inotify(exc: BaseException) -> None:
    """watchdog's Inotify.__init__ does not close its inotify fd when adding
    watches fails part-way, so the watches already added would keep eating the
    per-user limit (starving other programs) until we exit. Close it."""
    tb = exc.__traceback__
    while tb is not None:
        obj = tb.tb_frame.f_locals.get("self")
        if type(obj).__name__ == "Inotify" and not getattr(obj, "_closed", True):
            for name in ("_inotify_fd", "_kill_r", "_kill_w"):
                fd = getattr(obj, name, None)
                if isinstance(fd, int):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            obj._closed = True
        tb = tb.tb_next


def _rel_parts(path: str, base: str) -> List[str]:
    return split_path(os.path.normpath(path))[len(split_path(os.path.normpath(base))):]


class DebouncedEventHandler(FileSystemEventHandler):
    """Collects raw watchdog events and flushes them after a quiet period.

    On flush every touched path is judged by its *final* state on disk (exists
    -> create/modify, missing -> delete), so the common "save = delete + rename"
    pattern and create-then-delete bursts can never leave the index wrong.
    A continuous stream of events cannot postpone the flush forever
    (``max_wait_seconds``).
    """

    def __init__(
        self,
        on_created: Optional[Callable[[str], None]] = None,
        on_deleted: Optional[Callable[[str], None]] = None,
        on_modified: Optional[Callable[[str], None]] = None,
        on_moved: Optional[Callable[[str, str], None]] = None,
        debounce_seconds: float = 2.0,
        exclude_patterns: Optional[List[str]] = None,
        max_wait_seconds: float = 15.0,
        ignore: Optional[Callable[[str], bool]] = None,
        normalize: Optional[Callable[[str], str]] = None,
    ):
        super().__init__()
        self._on_created = on_created
        self._on_deleted = on_deleted
        self._on_modified = on_modified
        self._on_moved = on_moved
        self._debounce_seconds = debounce_seconds
        self._max_wait_seconds = max_wait_seconds
        self._matcher = ExcludeMatcher(exclude_patterns)
        self._ignore = ignore
        self._normalize = normalize
        self._timer: Optional[threading.Timer] = None
        self._first_event: Optional[float] = None
        self._cancelled = False
        self._lock = threading.RLock()
        self._pending_created: Set[str] = set()
        self._pending_deleted: Set[str] = set()
        self._pending_modified: Set[str] = set()
        self._pending_moved: List[tuple] = []

    def _should_exclude(self, path: str) -> bool:
        if self._ignore is not None:
            try:
                if self._ignore(path):
                    return True
            except Exception:
                pass
        return bool(self._matcher) and self._matcher.matches_path(path)

    def _reset_timer(self) -> None:
        """Caller holds ``self._lock``."""
        if self._cancelled:
            return
        now = time.monotonic()
        if self._first_event is None:
            self._first_event = now
        if self._timer is not None:
            self._timer.cancel()
        remaining = self._max_wait_seconds - (now - self._first_event)
        delay = min(self._debounce_seconds, max(0.0, remaining))
        self._timer = threading.Timer(delay, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self) -> None:
        """Drop pending events and stop the timer (used when the watcher stops)."""
        with self._lock:
            self._cancelled = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending_created.clear()
            self._pending_deleted.clear()
            self._pending_modified.clear()
            self._pending_moved.clear()
            self._first_event = None

    def _flush(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            created = set(self._pending_created)
            deleted = set(self._pending_deleted)
            modified = set(self._pending_modified)
            moved = list(self._pending_moved)
            self._pending_created.clear()
            self._pending_deleted.clear()
            self._pending_modified.clear()
            self._pending_moved.clear()
            self._timer = None
            self._first_event = None

        # Callbacks run outside the lock so a slow DB write never blocks the
        # watchdog observer thread, and one failing callback cannot lose the rest.
        for src, dst in moved:
            if self._on_moved and not (self._should_exclude(src) and self._should_exclude(dst)):
                self._safe(self._on_moved, src, dst)

        for path in sorted(created | deleted | modified):
            if self._should_exclude(path):
                continue
            if os.path.lexists(path):
                if path in created or path in deleted:
                    cb = self._on_created
                else:
                    if os.path.isdir(path):
                        continue  # folder mtime changes carry no information
                    cb = self._on_modified
            else:
                cb = self._on_deleted
            if cb:
                self._safe(cb, path)

    @staticmethod
    def _safe(cb: Callable, *args) -> None:
        try:
            cb(*args)
        except Exception:
            pass

    def _norm(self, path) -> str:
        path = os.fsdecode(path)
        if self._normalize is not None:
            try:
                return self._normalize(path)
            except Exception:
                pass
        return path

    def _queue(self, bucket: Set[str], path: str) -> None:
        with self._lock:
            if self._cancelled:
                return
            bucket.add(self._norm(path))
            self._reset_timer()

    def on_created(self, event: FileSystemEvent) -> None:
        self._queue(self._pending_created, event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._queue(self._pending_deleted, event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._queue(self._pending_modified, event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._pending_moved.append(
                (self._norm(event.src_path), self._norm(event.dest_path))
            )
            self._reset_timer()


class FileWatcher:
    def __init__(
        self,
        persistent_index: Optional[PersistentIndex] = None,
        config: Optional[AppConfig] = None,
    ):
        self.persistent_index = persistent_index or PersistentIndex()
        self.config = config or AppConfig()
        self._observer: Optional[Observer] = None
        self._handler: Optional[DebouncedEventHandler] = None
        self._is_running = False
        self._watched_paths: Set[str] = set()
        self._lock = threading.RLock()
        self._change_callback: Optional[Callable[[str, str], None]] = None
        self._error_callback: Optional[Callable[[Exception], None]] = None
        self._excludes = ExcludeMatcher(self.config.exclude_patterns)
        # (alias, root): another spelling of a watched root that events may use,
        # e.g. macOS FSEvents reports /private/var/... for a root under /var.
        self._aliases: List[Tuple[str, str]] = []
        self._limit_logged = False

    def on_change(self, cb: Callable[[str, str], None]) -> None:
        self._change_callback = cb

    def on_error(self, cb: Callable[[Exception], None]) -> None:
        self._error_callback = cb

    # ------------------------------------------------------------------ filtering

    def _is_ignored(self, path: str) -> bool:
        """Exclusion is judged on the part of the path *below* the watched root
        (same rule as the indexer), plus the index's own database files.
        "Below the root" follows the platform's case rules."""
        if self.persistent_index.is_own_file(path):
            return True
        with self._lock:
            roots = list(self._watched_paths)
        for root in roots:
            if is_path_under(path, root):
                rel_parts = _rel_parts(path, root)
                break
        else:
            rel_parts = split_path(path)
        for part in rel_parts:
            if is_system_name(part) or self._excludes.matches_name(part):
                return True
        return bool(self._excludes.sequences) and self._excludes.matches_path(path)

    def _normalize_event_path(self, path: str) -> str:
        """Spell an event path the way the indexer does (under the configured
        root string), so watcher updates never create duplicate rows for
        /private/var vs /var (macOS) or a differently-cased root."""
        with self._lock:
            pairs = [(r, r) for r in self._watched_paths] + list(self._aliases)
        for alias, root in pairs:
            if is_path_under(path, alias):
                rel = _rel_parts(path, alias)
                return os.path.join(root, *rel) if rel else root
        return path

    # ------------------------------------------------------------------ index updates

    def _emit(self, event_type: str, path: str) -> None:
        if self._change_callback:
            try:
                self._change_callback(event_type, path)
            except Exception as exc:
                self._report(exc)

    def _report(self, exc: Exception) -> None:
        if self._error_callback:
            try:
                self._error_callback(exc)
            except Exception:
                pass

    def _upsert_path(self, path: str) -> bool:
        """Index ``path`` (and, for a folder, everything below it). Returns False
        if the path vanished in the meantime."""
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        is_dir = os.path.isdir(path) and not os.path.islink(path)
        self.persistent_index.add_file(build_file_info(path, st, is_dir))
        if is_dir:
            self._index_tree(path)
        return True

    def _index_tree(self, root: str) -> None:
        batch: List[dict] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [
                d for d in dirnames
                if not self._is_ignored(os.path.join(dirpath, d))
                and not os.path.islink(os.path.join(dirpath, d))
            ]
            for name in list(dirnames) + filenames:
                full = os.path.join(dirpath, name)
                if self._is_ignored(full):
                    continue
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                batch.append(build_file_info(full, st, name in dirnames))
                if len(batch) >= _TREE_BATCH:
                    self.persistent_index.add_files_batch(batch)
                    batch = []
        if batch:
            self.persistent_index.add_files_batch(batch)

    def _report_limit(self, root: str, exc: OSError) -> None:
        """Log/report the OS watch limit once; the root simply gets no live updates."""
        if self._limit_logged:
            return
        self._limit_logged = True
        _log.warning(
            "Live file watching disabled for %s (%s). The index is still kept up "
            "to date by periodic rescans. On Linux raise fs.inotify.max_user_watches "
            "/ max_user_instances to enable it.", root, exc,
        )
        self._report(exc)

    def _handle_created(self, path: str) -> None:
        try:
            if self._upsert_path(path):
                self._emit("created", path)
        except Exception as exc:
            self._report(exc)

    def _handle_deleted(self, path: str) -> None:
        try:
            self.persistent_index.remove_file(path)  # also removes a folder's subtree
            self._emit("deleted", path)
        except Exception as exc:
            self._report(exc)

    def _handle_modified(self, path: str) -> None:
        try:
            if self._upsert_path(path):
                self._emit("modified", path)
        except Exception as exc:
            self._report(exc)

    def _handle_moved(self, src_path: str, dest_path: str) -> None:
        try:
            self.persistent_index.remove_file(src_path)
            if not self._is_ignored(dest_path):
                self._upsert_path(dest_path)
            self._emit("moved", f"{src_path} -> {dest_path}")
        except Exception as exc:
            self._report(exc)

    # ------------------------------------------------------------------ lifecycle

    def watch(self, path: str) -> None:
        with self._lock:
            if not os.path.isdir(path):
                raise NotADirectoryError(f"Not a directory: {path}")
            self._watched_paths.add(path)

    def unwatch(self, path: str) -> None:
        with self._lock:
            self._watched_paths.discard(path)

    def set_paths(self, paths: Iterable[str]) -> None:
        """Replace the watched set with the given folders (missing ones are skipped,
        duplicates and folders nested in another watched folder are dropped)."""
        with self._lock:
            self._watched_paths = set(dedupe_roots(p for p in paths if os.path.isdir(p)))

    def start(self) -> None:
        with self._lock:
            if self._is_running:
                return
            if not self._watched_paths:
                return
            self._excludes = ExcludeMatcher(self.config.exclude_patterns)
            self._aliases = []
            for root in self._watched_paths:
                real = os.path.realpath(root)
                if not is_path_under(real, root) or not is_path_under(root, real):
                    self._aliases.append((real, root))
            handler = DebouncedEventHandler(
                on_created=self._handle_created,
                on_deleted=self._handle_deleted,
                on_modified=self._handle_modified,
                on_moved=self._handle_moved,
                debounce_seconds=2.0,
                ignore=self._is_ignored,
                normalize=self._normalize_event_path,
            )
            observer = Observer()
            observer.daemon = True
            try:
                observer.start()
            except Exception as exc:
                self._report(exc)
                return
            # Scheduling on a running observer starts each root's emitter right
            # away, so one root failing (e.g. inotify limits) cannot take the
            # other roots down with it.
            scheduled = 0
            for wp in sorted(self._watched_paths):
                try:
                    observer.schedule(handler, wp, recursive=True)
                    scheduled += 1
                except OSError as exc:
                    if exc.errno in _LIMIT_ERRNOS:
                        _close_leaked_inotify(exc)
                        self._report_limit(wp, exc)
                    else:
                        self._report(exc)
                except Exception as exc:
                    self._report(exc)
            if not scheduled:
                observer.stop()
                return
            self._observer = observer
            self._handler = handler
            self._is_running = True

    def stop(self) -> None:
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False
            observer, self._observer = self._observer, None
            handler, self._handler = self._handler, None
        if handler is not None:
            handler.cancel()
        if observer is not None:
            observer.stop()
            if observer.is_alive():
                observer.join(timeout=5)

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def watched_paths(self) -> List[str]:
        with self._lock:
            return list(self._watched_paths)

    def __enter__(self) -> FileWatcher:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()
