from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Set

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import AppConfig
from .persistent_index import PersistentIndex
from .utils.file_utils import FileHash


class DebouncedEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        on_created: Optional[Callable[[str], None]] = None,
        on_deleted: Optional[Callable[[str], None]] = None,
        on_modified: Optional[Callable[[str], None]] = None,
        on_moved: Optional[Callable[[str, str], None]] = None,
        debounce_seconds: float = 2.0,
        exclude_patterns: Optional[List[str]] = None,
    ):
        super().__init__()
        self._on_created = on_created
        self._on_deleted = on_deleted
        self._on_modified = on_modified
        self._on_moved = on_moved
        self._debounce_seconds = debounce_seconds
        self._exclude_patterns = exclude_patterns or []
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.RLock()
        self._pending_created: Set[str] = set()
        self._pending_deleted: Set[str] = set()
        self._pending_modified: Set[str] = set()
        self._pending_moved: List[tuple[str, str]] = []

    def _should_exclude(self, path: str) -> bool:
        p = Path(path)
        for pattern in self._exclude_patterns:
            if pattern in p.parts:
                return True
        return False

    def _reset_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce_seconds, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            for path in self._pending_created:
                if not self._should_exclude(path) and self._on_created:
                    self._on_created(path)
            for path in self._pending_deleted:
                if not self._should_exclude(path) and self._on_deleted:
                    self._on_deleted(path)
            for path in self._pending_modified:
                if not self._should_exclude(path) and self._on_modified:
                    self._on_modified(path)
            for src, dst in self._pending_moved:
                if self._on_moved:
                    self._on_moved(src, dst)
            self._pending_created.clear()
            self._pending_deleted.clear()
            self._pending_modified.clear()
            self._pending_moved.clear()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        with self._lock:
            self._pending_created.add(event.src_path)
            self._reset_timer()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        with self._lock:
            self._pending_deleted.add(event.src_path)
            self._reset_timer()

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        with self._lock:
            self._pending_modified.add(event.src_path)
            self._reset_timer()

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        with self._lock:
            self._pending_moved.append((event.src_path, event.dest_path))
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
        self._thread: Optional[threading.Thread] = None
        self._is_running = False
        self._watched_paths: Set[str] = set()
        self._lock = threading.RLock()
        self._change_callback: Optional[Callable[[str, str], None]] = None
        self._error_callback: Optional[Callable[[Exception], None]] = None
        self._file_hashes: dict = {}

    def on_change(self, cb: Callable[[str, str], None]) -> None:
        self._change_callback = cb

    def on_error(self, cb: Callable[[Exception], None]) -> None:
        self._error_callback = cb

    def _handle_created(self, path: str) -> None:
        try:
            p = Path(path)
            if not p.exists():
                return
            st = p.stat()
            parent = str(p.parent)
            file_info = {
                "path": path,
                "name": p.name,
                "extension": p.suffix.lower() or "File",
                "size": st.st_size,
                "modified": int(st.st_mtime),
                "created": int(st.st_ctime),
                "is_folder": p.is_dir(),
                "parent_path": parent,
            }
            self.persistent_index.add_file(file_info)
            if self._change_callback:
                self._change_callback("created", path)
        except Exception as exc:
            if self._error_callback:
                self._error_callback(exc)

    def _handle_deleted(self, path: str) -> None:
        try:
            self.persistent_index.remove_file(path)
            self._file_hashes.pop(path, None)
            if self._change_callback:
                self._change_callback("deleted", path)
        except Exception as exc:
            if self._error_callback:
                self._error_callback(exc)

    def _handle_modified(self, path: str) -> None:
        try:
            p = Path(path)
            if not p.exists():
                return
            st = p.stat()
            parent = str(p.parent)
            file_info = {
                "path": path,
                "name": p.name,
                "extension": p.suffix.lower() or "File",
                "size": st.st_size,
                "modified": int(st.st_mtime),
                "created": int(st.st_ctime),
                "is_folder": p.is_dir(),
                "parent_path": parent,
            }
            self.persistent_index.add_file(file_info)
            if self._change_callback:
                self._change_callback("modified", path)
        except Exception as exc:
            if self._error_callback:
                self._error_callback(exc)

    def _handle_moved(self, src_path: str, dest_path: str) -> None:
        try:
            self.persistent_index.remove_file(src_path)
            self._file_hashes.pop(src_path, None)
            p = Path(dest_path)
            if p.exists():
                st = p.stat()
                parent = str(p.parent)
                file_info = {
                    "path": dest_path,
                    "name": p.name,
                    "extension": p.suffix.lower() or "File",
                    "size": st.st_size,
                    "modified": int(st.st_mtime),
                    "created": int(st.st_ctime),
                    "is_folder": p.is_dir(),
                    "parent_path": parent,
                }
                self.persistent_index.add_file(file_info)
            if self._change_callback:
                self._change_callback("moved", f"{src_path} -> {dest_path}")
        except Exception as exc:
            if self._error_callback:
                self._error_callback(exc)

    def watch(self, path: str) -> None:
        with self._lock:
            if not os.path.isdir(path):
                raise NotADirectoryError(f"Not a directory: {path}")
            self._watched_paths.add(path)

    def unwatch(self, path: str) -> None:
        with self._lock:
            self._watched_paths.discard(path)

    def start(self) -> None:
        with self._lock:
            if self._is_running:
                return
            if not self._watched_paths:
                return
            self._is_running = True

            event_handler = DebouncedEventHandler(
                on_created=self._handle_created,
                on_deleted=self._handle_deleted,
                on_modified=self._handle_modified,
                on_moved=self._handle_moved,
                debounce_seconds=2.0,
                exclude_patterns=list(self.config.exclude_patterns),
            )

            self._observer = Observer()
            for wp in self._watched_paths:
                self._observer.schedule(event_handler, wp, recursive=True)

            self._observer.start()

    def stop(self) -> None:
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=5)
                self._observer = None

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
