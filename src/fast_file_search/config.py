from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List

from .utils.platform_utils import get_app_data_dir, path_key


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


@dataclass
class AppConfig:
    index_paths: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=lambda: [".git", "__pycache__"])
    max_index_size: int = 500  # MB
    auto_index_interval: int = 300  # seconds
    theme: str = "light"
    max_results: int = 1000
    search_history: List[str] = field(default_factory=list)
    # Extract and store file *contents* in the index (slow, grows the DB a lot).
    index_content: bool = False

    def __post_init__(self) -> None:
        # Hand-edited / corrupt JSON must not crash startup: coerce bad types.
        for name in ("index_paths", "exclude_patterns", "search_history"):
            value = getattr(self, name)
            if not isinstance(value, list):
                value = []
            setattr(self, name, [v for v in value if isinstance(v, str)])
        # One folder listed twice (e.g. "C:\Docs" and "c:\docs" on Windows/macOS)
        # would be indexed twice under two spellings: keep the first only.
        seen = set()
        unique: List[str] = []
        for p in self.index_paths:
            key = path_key(p) if p else p
            if p and key not in seen:
                seen.add(key)
                unique.append(p)
        self.index_paths = unique
        for name, default in (("max_index_size", 500), ("auto_index_interval", 300),
                              ("max_results", 1000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                setattr(self, name, default)
        if self.theme not in ("light", "dark"):
            self.theme = "light"
        self.index_content = bool(self.index_content)

    def _get_config_dir(self) -> Path:
        try:
            return get_app_data_dir("Fast File Search Pro")
        except Exception:
            return Path.home() / ".fast-file-search"

    def _get_config_path(self) -> Path:
        return self._get_config_dir() / "config.json"

    def save(self) -> None:
        path = self._get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(asdict(self), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        path = cfg._get_config_path()
        if not path.exists():
            import warnings
            warnings.warn(f"Config file not found at {path}, using defaults.")
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            merged = asdict(cfg)
            merged.update({k: v for k, v in data.items() if k in merged})
            return cls(**merged)
        except Exception:
            import warnings
            warnings.warn(f"Failed to load config from {path}, using defaults.")
            return AppConfig()


class ConfigManager:
    """Load/save AppConfig to a JSON file and notify listeners on changes."""

    _lock = threading.RLock()

    def __init__(self, app_name: str = "Fast File Search Pro"):
        self.app_name = app_name
        self.config_dir = get_app_data_dir(app_name)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.json"
        self._listeners: List[Callable[[AppConfig], None]] = []
        self.config = AppConfig()
        # True only when no config file existed yet (a genuine first launch).
        self.first_run = False
        self.load()

    def _notify(self) -> None:
        # Call listeners with a snapshot of the config
        snapshot = AppConfig(**asdict(self.config))
        for cb in list(self._listeners):
            try:
                cb(snapshot)
            except Exception:
                # Listener errors should not break config flow
                continue

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
            if not self.config_file.exists():
                self.first_run = True
                self.save()  # create defaults
                return
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                # Protect against missing keys and enforce types via dataclass
                merged = asdict(self.config)
                merged.update({k: v for k, v in data.items() if k in merged})
                cfg = AppConfig(**merged)
                # enforce search_history max 50
                cfg.search_history = (cfg.search_history or [])[-50:]
                self.config = cfg
            except Exception:
                # Corrupt file: back it up and write defaults
                try:
                    backup = self.config_file.with_suffix(".corrupt.json")
                    self.config_file.replace(backup)
                except Exception:
                    pass
                self.config = AppConfig()
                self.save()
            finally:
                self._notify()

    def save(self) -> None:
        with self._lock:
            try:
                # ensure history length
                self.config.search_history = (self.config.search_history or [])[-50:]
                data = json.dumps(asdict(self.config), indent=2, ensure_ascii=False)
                _atomic_write_text(self.config_file, data)
            except Exception as exc:
                raise IOError(f"Failed to save config: {exc}") from exc
            finally:
                self._notify()

    def add_search_history(self, term: str) -> None:
        with self._lock:
            if not term:
                return
            # dedupe recent identical term
            hist = [t for t in self.config.search_history if t != term]
            hist.append(term)
            self.config.search_history = hist[-50:]
            self.save()


# Convenience singleton for app usage
config_manager = ConfigManager()
