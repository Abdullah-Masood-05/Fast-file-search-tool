"""Logging and crash capture, one log file per process (design G.1).

``setup_logging("gui" | "engine", log_dir)`` is the only entry point most code needs.
It installs a rotating file handler (1 MB x 5), ``sys.excepthook``,
``threading.excepthook``, ``sys.unraisablehook`` and ``faulthandler``.
No Qt import at module level: ``install_qt_message_handler`` imports it lazily.
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, List, Optional

MAX_BYTES = 1_000_000
BACKUP_COUNT = 5

_log = logging.getLogger("fast_file_search.applog")
_state_lock = threading.RLock()
_handler: Optional[logging.Handler] = None
_crash_file = None  # kept open for faulthandler's lifetime
_listeners: List[Callable[[type, BaseException, object, str], None]] = []
_prev_hooks: dict = {}
_process = "gui"

# Qt message categories that only add noise.
_QT_NOISE = (
    "qt.qpa.fonts",
    "qt.qpa.mime",
    "qt.accessibility",
    "qt.svg",
    "propagateSizeHints",
)


def add_exception_listener(fn: Callable[[type, BaseException, object, str], None]) -> None:
    """Call ``fn(exc_type, exc, tb, thread_name)`` after an unhandled exception is
    logged (GUI: show the "Something went wrong" dialog; engine: send ``Fatal`` and exit).
    Listener errors are logged and swallowed."""
    with _state_lock:
        if fn not in _listeners:
            _listeners.append(fn)


def remove_exception_listener(fn: Callable) -> None:
    with _state_lock:
        if fn in _listeners:
            _listeners.remove(fn)


def _notify(exc_type, exc, tb, thread_name: str) -> None:
    for fn in list(_listeners):
        try:
            fn(exc_type, exc, tb, thread_name)
        except Exception:
            _log.exception("exception listener failed")


def format_exception(exc_type, exc, tb) -> str:
    return "".join(traceback.format_exception(exc_type, exc, tb))


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _log.critical("Unhandled exception in main thread\n%s", format_exception(exc_type, exc, tb))
    _notify(exc_type, exc, tb, "MainThread")


def _threading_hook(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "?"
    _log.critical("Unhandled exception in thread %s\n%s", name,
                  format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _notify(args.exc_type, args.exc_value, args.exc_traceback, name)


def _unraisable_hook(args) -> None:
    where = repr(args.object) if args.object is not None else "?"
    _log.error("Unraisable exception in %s: %s\n%s", where, args.err_msg or "",
               format_exception(args.exc_type, args.exc_value, args.exc_traceback))


def header_lines(process: str) -> List[str]:
    """Version/environment lines written at the top of each log session."""
    try:
        from . import __version__ as version
    except Exception:
        version = "?"
    lines = [
        f"Fast File Search Pro {version} ({process}) pid={os.getpid()}",
        f"Python {platform.python_version()} {platform.python_implementation()}",
        f"OS {platform.system()} {platform.release()} {platform.machine()}",
        f"frozen={getattr(sys, 'frozen', False)} compiled={'__compiled__' in globals()}",
    ]
    if process == "gui":
        try:
            import PySide6
            from PySide6 import QtCore
            lines.append(f"PySide6 {PySide6.__version__} Qt {QtCore.qVersion()}")
        except Exception:
            lines.append("Qt unavailable")
    else:
        try:  # the engine process owns SQLite; the GUI never imports it
            import sqlite3
            lines.append(f"SQLite {sqlite3.sqlite_version}")
        except Exception:
            pass
        for mod, attr in (("textfold", "FOLD_VERSION"), ("store", "SCHEMA_VERSION")):
            try:
                m = __import__(f"fast_file_search.engine.{mod}", fromlist=[attr])
                lines.append(f"{attr}={getattr(m, attr)}")
            except Exception:
                pass
    return lines


def setup_logging(process: str, log_dir: Path) -> None:
    """Configure logging for this process. Safe to call more than once (the
    previous handler is replaced). ``FFS_DEBUG=1`` switches to DEBUG."""
    global _handler, _crash_file, _process
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        _process = process
        root = logging.getLogger()
        if _handler is not None:
            root.removeHandler(_handler)
            try:
                _handler.close()
            except Exception:
                pass
        handler = logging.handlers.RotatingFileHandler(
            log_dir / f"{process}.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
            encoding="utf-8", delay=False)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(threadName)s %(name)s: %(message)s"))
        level = logging.DEBUG if os.environ.get("FFS_DEBUG") == "1" else logging.INFO
        handler.setLevel(level)
        root.addHandler(handler)
        root.setLevel(level)
        _handler = handler

        for line in header_lines(process):
            _log.info(line)

        # Crash capture.
        if not _prev_hooks:
            _prev_hooks["excepthook"] = sys.excepthook
            _prev_hooks["threading"] = threading.excepthook
            _prev_hooks["unraisable"] = sys.unraisablehook
        sys.excepthook = _excepthook
        threading.excepthook = _threading_hook
        sys.unraisablehook = _unraisable_hook

        try:
            new_crash = open(log_dir / f"{process}-crash.log", "a", encoding="utf-8")
            faulthandler.enable(new_crash, all_threads=True)
            old, _crash_file = _crash_file, new_crash
            if old is not None:
                old.close()
        except Exception:
            _log.exception("could not enable faulthandler")


def shutdown_logging() -> None:
    """Undo ``setup_logging`` (tests, in-process engine)."""
    global _handler, _crash_file
    with _state_lock:
        if _prev_hooks:
            sys.excepthook = _prev_hooks.pop("excepthook")
            threading.excepthook = _prev_hooks.pop("threading")
            sys.unraisablehook = _prev_hooks.pop("unraisable")
        if _handler is not None:
            logging.getLogger().removeHandler(_handler)
            _handler.close()
            _handler = None
        try:
            faulthandler.disable()
        except Exception:
            pass
        if _crash_file is not None:
            _crash_file.close()
            _crash_file = None
        _listeners.clear()


def log_dir_of_handler() -> Optional[Path]:
    h = _handler
    return Path(h.baseFilename).parent if isinstance(h, logging.handlers.RotatingFileHandler) else None


def tail_log(lines: int = 50) -> str:
    """Last ``lines`` lines of this process's log (for "Copy diagnostics")."""
    h = _handler
    if not isinstance(h, logging.handlers.RotatingFileHandler):
        return ""
    try:
        h.flush()
        with open(h.baseFilename, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""


def qt_message_to_logging(msg_type, context, message: str) -> None:
    """Map a Qt message to ``logging``; drops known-noise categories."""
    category = getattr(context, "category", None) or ""
    if any(category.startswith(n) or n in message for n in _QT_NOISE):
        return
    name = getattr(msg_type, "name", str(msg_type))
    level = {
        "QtDebugMsg": logging.DEBUG,
        "QtInfoMsg": logging.INFO,
        "QtWarningMsg": logging.WARNING,
        "QtCriticalMsg": logging.ERROR,
        "QtSystemMsg": logging.ERROR,
        "QtFatalMsg": logging.CRITICAL,
    }.get(name, logging.WARNING)
    logging.getLogger("qt").log(level, "%s%s", f"[{category}] " if category else "", message)


def install_qt_message_handler() -> None:
    """GUI only: route Qt warnings into the log (imports Qt lazily)."""
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(qt_message_to_logging)
