"""Application settings: typed v2 schema, validation, v1 migration (design G.3).

Nothing here touches the file system at import time. Use ``get_config_manager()``.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .engine.protocol import EngineBootstrap, EngineConfig, Notice
from .utils.platform_utils import (
    APP_DIR_NAME,
    dedupe_roots,
    get_app_data_dir,
    get_cache_dir,
    get_data_dir,
    get_log_dir,
    get_state_dir,
    paths_equal,
)

log = logging.getLogger(__name__)

CONFIG_VERSION = 2

#: Name exclusions merged into ``exclude_patterns`` once, at the v1 -> v2 migration.
DEFAULT_EXCLUDES: Tuple[str, ...] = (
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".gradle", ".npm", ".yarn", ".pnpm-store", ".cache",
)

#: v1 keys that no longer exist (dropped by the migration, not preserved).
_DROPPED_V1_KEYS = ("max_results", "max_index_size", "auto_index_interval")

MIN_RESCAN_MINUTES, MAX_RESCAN_MINUTES, DEFAULT_RESCAN_MINUTES = 5, 1440, 30
HISTORY_LIMIT = 50

HOME_EXCLUDES_TEXT = (
    "Library, AppData and caches inside your home folder are now skipped. "
    "Change this in Settings."
)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a truncated file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- validators

def _str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _unique_strs(values: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _clamped_int(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(lo, min(hi, value))


@dataclass
class AppConfig:
    config_version: int = CONFIG_VERSION
    index_paths: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    removed_default_excludes: List[str] = field(default_factory=list)
    include_home_library: bool = False
    index_hidden: bool = True
    descend_packages: bool = False
    live_monitoring: bool = True
    pause_on_battery: bool = True
    periodic_rescan_minutes: int = DEFAULT_RESCAN_MINUTES
    # Kept for compatibility, ignored by the 2.0 engine.
    index_content: bool = False
    theme: str = "light"
    search_history: List[str] = field(default_factory=list)
    #: Keys this version does not know. Written back unchanged on save.
    extras: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Hand-edited / corrupt JSON must not crash startup: coerce bad types.
        if not isinstance(self.config_version, int) or isinstance(self.config_version, bool):
            self.config_version = CONFIG_VERSION
        paths = _unique_strs(_str_list(self.index_paths))
        # Duplicates (by platform case rules) and nested roots are dropped.
        self.index_paths = dedupe_roots(paths)
        self.exclude_patterns = _unique_strs(_str_list(self.exclude_patterns))
        # A default the user deleted is remembered; one they re-added is forgotten.
        gone = [d for d in DEFAULT_EXCLUDES if d not in self.exclude_patterns]
        self.removed_default_excludes = [
            d for d in _unique_strs(_str_list(self.removed_default_excludes) + gone)
            if d not in self.exclude_patterns
        ]
        self.include_home_library = _bool(self.include_home_library, False)
        self.index_hidden = _bool(self.index_hidden, True)
        self.descend_packages = _bool(self.descend_packages, False)
        self.live_monitoring = _bool(self.live_monitoring, True)
        self.pause_on_battery = _bool(self.pause_on_battery, True)
        self.periodic_rescan_minutes = _clamped_int(
            self.periodic_rescan_minutes, DEFAULT_RESCAN_MINUTES,
            MIN_RESCAN_MINUTES, MAX_RESCAN_MINUTES)
        self.index_content = _bool(self.index_content, False)
        if self.theme not in ("light", "dark"):
            self.theme = "light"
        self.search_history = _str_list(self.search_history)[-HISTORY_LIMIT:]
        if not isinstance(self.extras, dict):
            self.extras = {}

    # -- serialisation
    @classmethod
    def known_keys(cls) -> set:
        return {f.name for f in fields(cls)} - {"extras"}

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.extras)
        for name in self.known_keys():
            data[name] = copy.deepcopy(getattr(self, name))
        data["config_version"] = CONFIG_VERSION
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        """Build from a v2 dict: bad values fall back to defaults, unknown keys go to ``extras``."""
        known = cls.known_keys()
        kwargs = {k: v for k, v in data.items() if k in known}
        extras = {k: v for k, v in data.items() if k not in known}
        return cls(**kwargs, extras=extras)

    def snapshot(self) -> "AppConfig":
        return copy.deepcopy(self)

    def to_engine_config(self) -> EngineConfig:
        return EngineConfig(
            roots=tuple(self.index_paths),
            exclude_patterns=tuple(self.exclude_patterns),
            include_home_library=self.include_home_library,
            index_hidden=self.index_hidden,
            descend_packages=self.descend_packages,
            live_monitoring=self.live_monitoring,
            pause_on_battery=self.pause_on_battery,
            periodic_rescan_minutes=self.periodic_rescan_minutes,
        )

    # -- legacy convenience (v1 API)
    def _get_config_path(self) -> Path:
        return _config_dir() / "config.json"

    def save(self) -> None:
        path = self._get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls) -> "AppConfig":
        return _read_file(_config_dir() / "config.json")[0]


def _config_dir() -> Path:
    try:
        return get_app_data_dir(APP_DIR_NAME)
    except Exception:
        return Path.home() / ".fast-file-search"


# ---------------------------------------------------------------- migration

def migrate(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Notice]]:
    """Upgrade a parsed config dict to the current schema. Pure: no file access except
    comparing roots with the home folder."""
    notices: List[Notice] = []
    data = dict(data)
    version = data.get("config_version")
    if not isinstance(version, int) or isinstance(version, bool):
        version = 1
    if version >= CONFIG_VERSION:
        return data, notices

    for key in _DROPPED_V1_KEYS:
        data.pop(key, None)
    patterns = _unique_strs(_str_list(data.get("exclude_patterns", [".git", "__pycache__"])))
    data["exclude_patterns"] = patterns + [d for d in DEFAULT_EXCLUDES if d not in patterns]
    data["removed_default_excludes"] = []
    data["config_version"] = CONFIG_VERSION
    if data.get("index_content") is True:
        log.warning("index_content is ignored in 2.0 (file contents are not indexed)")

    home = str(Path.home())
    if any(p and paths_equal(p, home) for p in _str_list(data.get("index_paths"))):
        notices.append(Notice("info", "home_excludes_added", HOME_EXCLUDES_TEXT))
    return data, notices


def _read_file(path: Path) -> Tuple[AppConfig, bool, List[Notice], bool]:
    """Returns (config, first_run, notices, needs_save)."""
    notices: List[Notice] = []
    if not path.exists():
        return AppConfig(), True, notices, True
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("config root is not an object")
    except (OSError, ValueError) as exc:
        log.warning("config unreadable (%s); using defaults", exc)
        try:
            os.replace(path, path.with_suffix(".corrupt.json"))
        except OSError:
            pass
        notices.append(Notice(
            "warning", "config_recovered",
            "Your settings file could not be read, so default settings are in use. "
            "The old file was kept as config.corrupt.json."))
        return AppConfig(), False, notices, True
    old_version = raw.get("config_version")
    migrated, migration_notices = migrate(raw)
    notices.extend(migration_notices)
    needs_save = migrated.get("config_version") != old_version
    cfg = AppConfig.from_dict(migrated)
    if needs_save:
        # Keep the original once so a migration can be inspected or undone by hand.
        try:
            backup = path.with_name("config.v1.json")
            if not backup.exists():
                shutil.copy2(path, backup)
        except OSError:
            pass
    return cfg, False, notices, needs_save


class ConfigManager:
    """Load/save AppConfig to a JSON file and notify listeners on changes."""

    _lock = threading.RLock()

    def __init__(self, app_name: str = APP_DIR_NAME):
        self.app_name = app_name
        self.config_dir = get_app_data_dir(app_name)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.json"
        self._listeners: List[Callable[[AppConfig], None]] = []
        self.config = AppConfig()
        # True only when no config file existed yet (a genuine first launch).
        self.first_run = False
        #: Notices produced by load/migration, for the UI to show once (``take_notices``).
        self.notices: List[Notice] = []
        self.load()

    def take_notices(self) -> List[Notice]:
        with self._lock:
            out, self.notices = self.notices, []
            return out

    def _notify(self) -> None:
        snapshot = self.config.snapshot()
        for cb in list(self._listeners):
            try:
                cb(snapshot)
            except Exception:
                continue  # listener errors must not break config flow

    def register_listener(self, callback: Callable[[AppConfig], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unregister_listener(self, callback: Callable[[AppConfig], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def load(self) -> None:
        with self._lock:
            cfg, first_run, notices, needs_save = _read_file(self.config_file)
            self.config = cfg
            self.first_run = first_run
            self.notices.extend(notices)
            try:
                if needs_save:
                    self.save()
            finally:
                self._notify()

    def save(self) -> None:
        with self._lock:
            try:
                # Re-run validation so edits made through attributes are normalised.
                self.config = AppConfig.from_dict(self.config.to_dict())
                data = json.dumps(self.config.to_dict(), indent=2, ensure_ascii=False)
                self.config_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(self.config_file, data)
            except Exception as exc:
                raise IOError(f"Failed to save config: {exc}") from exc
            finally:
                self._notify()

    def add_search_history(self, term: str) -> None:
        with self._lock:
            if not term:
                return
            hist = [t for t in self.config.search_history if t != term]
            hist.append(term)
            self.config.search_history = hist[-HISTORY_LIMIT:]
            self.save()


# ---------------------------------------------------------------- singleton

_manager: Optional[ConfigManager] = None
_manager_lock = threading.Lock()


def get_config_manager() -> ConfigManager:
    """The process-wide ConfigManager, created on first use (never at import)."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ConfigManager()
        return _manager


def reset_config_manager() -> None:
    """Forget the singleton (tests)."""
    global _manager
    with _manager_lock:
        _manager = None


def __getattr__(name: str):
    # Backwards compatibility for ``from .config import config_manager``.
    if name == "config_manager":
        return get_config_manager()
    raise AttributeError(name)


# ---------------------------------------------------------------- engine bootstrap

def _app_version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:
        return ""


def build_engine_bootstrap(cfg: AppConfig) -> EngineBootstrap:
    """Resolve every directory once so the GUI and the engine process agree."""
    home = Path.home()
    own = [
        str(get_app_data_dir(APP_DIR_NAME)),   # config.json, Qt ui.ini (Windows, Linux)
        str(home / ".config" / APP_DIR_NAME),  # Qt IniFormat settings on macOS
        str(get_state_dir()),
    ]
    return EngineBootstrap(
        config=cfg.to_engine_config(),
        cache_dir=str(get_cache_dir(APP_DIR_NAME)),
        data_dir=str(get_data_dir(APP_DIR_NAME)),
        log_dir=str(get_log_dir()),
        own_paths=tuple(dict.fromkeys(own)),
        log_level="DEBUG" if os.environ.get("FFS_DEBUG") == "1" else "INFO",
        app_version=_app_version(),
    )
