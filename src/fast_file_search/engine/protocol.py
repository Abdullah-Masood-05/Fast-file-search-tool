"""GUI <-> engine messages. Pickled over a multiprocessing Pipe. No Qt, no sqlite3 here."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

PROTOCOL_VERSION = 1

# entry flags (also stored in SQLite entries.flags)
FOLDER, SYMLINK, HIDDEN, PACKAGE, DATALESS, SYSTEM, UNREADABLE, MOUNT = 1, 2, 4, 8, 16, 32, 64, 128

# ---------------------------------------------------------------- configuration
@dataclass(frozen=True)
class EngineConfig:
    roots: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    include_home_library: bool = False
    index_hidden: bool = True
    descend_packages: bool = False
    live_monitoring: bool = True
    pause_on_battery: bool = True
    periodic_rescan_minutes: int = 30

@dataclass(frozen=True)
class EngineBootstrap:
    config: EngineConfig
    cache_dir: str            # index-v5.db, snapshot, engine.lock
    data_dir: str             # search_history.db (+ .bak)
    log_dir: str
    own_paths: tuple[str, ...] = ()   # extra paths never indexed/watched (Qt settings dir, ...)
    log_level: str = "INFO"
    app_version: str = ""

# ---------------------------------------------------------------- search
@dataclass(frozen=True)
class FilterState:
    kinds: frozenset[str] = frozenset()      # subset of {'folder','file','image','document','code','audio','video','archive'}
    exts: frozenset[str] = frozenset()       # folded, no dot: {'pdf','docx'}
    size_min: Optional[int] = None           # bytes, inclusive; size filters match files only
    size_max: Optional[int] = None           # bytes, exclusive
    mtime_min: Optional[float] = None        # epoch seconds, inclusive (GUI resolves presets)
    mtime_max: Optional[float] = None        # exclusive
    root_ids: frozenset[int] = frozenset()   # empty = all roots
    folder: Optional[str] = None             # absolute folder scope (subtree)
    path_contains: Optional[str] = None
    excludes: tuple[str, ...] = ()           # names, globs, a/b sequences (same matcher as index excludes)
    favorites_only: bool = False
    def is_empty(self) -> bool:
        return self == FilterState()

@dataclass(frozen=True)
class SortSpec:
    key: str = "relevance"                   # relevance|name|folder|size|mtime|kind
    descending: bool = False

@dataclass(frozen=True)
class SearchRequest:
    gen: int
    text: str
    filters: FilterState = field(default_factory=FilterState)
    sort: SortSpec = field(default_factory=SortSpec)
    limit: int = 200
    suggestions: bool = True
    root_counts: bool = True
    commit_history: bool = False

@dataclass(frozen=True)
class FetchPage:
    gen: int
    offset: int
    limit: int = 500

class ResultRow(NamedTuple):
    id: int
    name: str                                 # original (raw) name
    path: str                                 # full path, usable for open/reveal
    root_id: int
    rel_dir: str                              # folder path relative to the root ('' = root itself)
    size: int
    mtime: int
    flags: int
    spans: tuple                              # ((start, end), ...) highlight ranges in `name`
    favorite: bool

class Suggestion(NamedTuple):
    kind: str                                 # 'history' | 'name'
    text: str
    count: int                                # hits under the current filters (>= 1)

@dataclass(frozen=True)
class SearchResult:
    gen: int
    total: int                                # exact, after all filters
    offset: int                               # 0 for the first page
    rows: tuple                               # tuple[ResultRow, ...]
    elapsed_ms: float
    root_counts: tuple = ()                   # ((root_id, count), ...) before the root scope
    suggestions: tuple = ()                   # tuple[Suggestion, ...]
    did_you_mean: Optional[str] = None
    error: Optional[str] = None
    index_generation: int = 0
    partial: bool = False                     # first scan still running
    refresh: bool = False                     # live refresh of the same gen (A.10)
    terms: tuple = ()                         # folded positive terms (for preview highlighting)

# ---------------------------------------------------------------- index control & status
@dataclass(frozen=True)
class Hello:
    protocol_version: int = PROTOCOL_VERSION

@dataclass(frozen=True)
class Configure:
    config: EngineConfig

@dataclass(frozen=True)
class IndexCommand:
    action: str                               # 'update' | 'rebuild' | 'rescan'
    root_ids: Optional[tuple] = None          # None = all roots

@dataclass(frozen=True)
class RootStatus:
    id: int
    path: str
    name: str                                 # display name (disambiguated)
    state: str                                # 'new'|'scanning'|'ok'|'denied'|'missing'|'error'
    entries: int
    folders: int
    bytes: int
    denied_dirs: int
    scanned_entries: int = 0                  # progress of the current scan
    expected_entries: int = 0                 # previous scan's count (0 = unknown)
    rate: float = 0.0                         # entries/s
    live: str = "on"                          # 'on' | 'limited' | 'off'
    last_scan_end: Optional[int] = None

@dataclass(frozen=True)
class IndexStatus:
    state: str                                # 'loading'|'scanning'|'idle'|'paused'|'error'
    roots: tuple                              # tuple[RootStatus, ...]
    total_entries: int
    total_folders: int
    live: str                                 # worst of the roots: 'on'|'limited'|'off'
    index_generation: int
    message: str = ""                         # e.g. "Waiting for permission to read Documents…"

@dataclass(frozen=True)
class Ready:
    protocol_version: int
    status: IndexStatus
    capabilities: tuple = ()                  # e.g. ('watchdog:fsevents', 'snapshot', 'sqlite:3.53.1')

@dataclass(frozen=True)
class IndexChanged:
    index_generation: int

# ---------------------------------------------------------------- user data
@dataclass(frozen=True)
class UserOp:
    op: str          # 'favorite'|'unfavorite'|'set_tags'|'set_note'|'forget'|'clear_history'|'pin'|'unpin'|'record_open'
    paths: tuple = ()
    value: object = None                      # tags list / note text / query text

@dataclass(frozen=True)
class ItemInfoRequest:
    req_id: int
    path: str

@dataclass(frozen=True)
class ItemInfo:
    req_id: int
    path: str
    favorite: bool
    tags: tuple
    note: str

# ---------------------------------------------------------------- misc
@dataclass(frozen=True)
class Notice:
    level: str        # 'info'|'warning'|'error'
    code: str         # 'tcc_denied','root_missing','inotify_limit','index_rebuilt','history_restored',
                      # 'config_recovered','home_excludes_added', ...
    text: str
    data: tuple = ()  # ((key, value), ...)

@dataclass(frozen=True)
class Fatal:
    text: str
    traceback: str

@dataclass(frozen=True)
class Shutdown:
    pass
