"""What is never indexed or watched, per OS (design C.3). No Qt, no sqlite3.

Everything is resolved once into frozensets / prefix tuples so the walker
(per entry) and the watcher handler (per event) stay at C-level checks.
All functions take an explicit ``platform`` so the per-OS rules can be tested
on any OS.
"""
from __future__ import annotations

import fnmatch
import ntpath
import os
import posixpath
import re
import sys
import unicodedata
from typing import Iterable, Optional, Sequence

# Names excluded anywhere in the tree, on every OS.
COMMON_NAMES = (
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".gradle", ".npm", ".yarn", ".pnpm-store", ".cache",
    ".Trash", ".Trashes", ".TemporaryItems", ".DS_Store", "Thumbs.db", "desktop.ini",
)

_MACOS_METADATA_NAMES = (
    ".Trash", ".Trashes", ".TemporaryItems", ".DS_Store", ".Spotlight-V100",
    ".fseventsd", ".DocumentRevisions-V100", ".MobileBackups", "Backups.backupdb",
)

# macOS packages: indexed as one leaf entry (flag PACKAGE), not descended.
PACKAGE_SUFFIXES = (
    ".app", ".appex", ".bundle", ".framework", ".kext", ".plugin", ".prefpane", ".qlgenerator",
    ".mdimporter", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary", ".fcpbundle",
    ".logicx", ".band", ".rtfd", ".pages", ".numbers", ".key", ".sparsebundle", ".vmwarevm", ".pvm",
    ".xcarchive", ".playground",
)

# The engine's own files (inside the cache / data dirs).
OWN_FILE_NAMES = ("index-v5.db", "index-v5.snap", "engine.lock", "search_history.db")


def _plat(platform: Optional[str]) -> str:
    return sys.platform if platform is None else platform


def folds_case(platform: Optional[str] = None) -> bool:
    """Windows (NTFS) and macOS (APFS/HFS+) compare names ignoring case."""
    return _plat(platform) in ("win32", "darwin")


def sep_of(platform: Optional[str] = None) -> str:
    return "\\" if _plat(platform) == "win32" else "/"


def system_folder_names(platform: Optional[str] = None) -> set:
    """Well-known OS folders never worth indexing (exact names anywhere). Windows
    names only count on Windows: elsewhere a folder called "Windows" is user data."""
    p = _plat(platform)
    if p == "win32":
        return {"$Recycle.Bin", "System Volume Information", "Windows", "WinSxS", "Program Files",
                "Program Files (x86)", ".Trash", ".Trashes", ".TemporaryItems"}
    if p == "darwin":
        return set(_MACOS_METADATA_NAMES)
    return {".Trash", ".Trashes", ".TemporaryItems", ".fseventsd", ".Spotlight-V100", "lost+found"}


def root_excludes(platform: Optional[str] = None) -> set:
    """Absolute paths skipped only when met *during* a walk (a root set to one of
    them still works). Windows' are relative to a drive root (see drive_root_names)."""
    p = _plat(platform)
    if p == "win32":
        return set()
    if p == "darwin":
        return {"/System", "/System/Volumes", "/Volumes", "/private/var", "/dev", "/.Spotlight-V100",
                "/.fseventsd", "/.vol", "/Network", "/cores"}
    return {"/proc", "/sys", "/dev", "/run", "/snap", "/tmp", "/var/cache", "/var/tmp"}


def drive_root_names(platform: Optional[str] = None) -> set:
    """Windows: names skipped directly under a drive root (X:\\<name>)."""
    if _plat(platform) == "win32":
        return {"Windows", "$Recycle.Bin", "System Volume Information"}
    return set()


def home_excludes(home: str, platform: Optional[str] = None, include_home_library: bool = False) -> list:
    """Folders under the home directory skipped when a root contains them."""
    p = _plat(platform)
    j = ntpath.join if p == "win32" else posixpath.join
    if p == "darwin":
        out = [j(home, ".Trash")]
        if include_home_library:
            # Even with ~/Library included, its caches, logs and sandbox containers are noise.
            lib = j(home, "Library")
            out += [j(lib, n) for n in ("Caches", "Logs", "Containers", "Group Containers", "Metadata",
                                        "Saved Application State", "WebKit", "HTTPStorages")]
        else:
            out.append(j(home, "Library"))
        return out
    if p == "win32":
        return [j(home, "AppData")]
    return [j(home, ".cache"), j(home, ".local", "share", "Trash")]


def name_key(name: str, platform: Optional[str] = None) -> str:
    """Comparison key for one name under the platform's case rules."""
    p = _plat(platform)
    if p == "win32":
        return name.lower() if name.isascii() else name.casefold()
    if p == "darwin":
        return name.lower() if name.isascii() else unicodedata.normalize("NFC", name).casefold()
    return name


def path_key(path: str, platform: Optional[str] = None) -> str:
    """Comparison key for a path: normalized separators, case-folded on Windows and
    macOS, NFC on macOS. Never ends with a separator except for a bare root."""
    p = _plat(platform)
    s = os.fspath(path)
    s = ntpath.normpath(s) if p == "win32" else posixpath.normpath(s)
    if p == "win32":
        return s.lower() if s.isascii() else s.casefold()
    if p == "darwin":
        return s.lower() if s.isascii() else unicodedata.normalize("NFC", s).casefold()
    return s


def is_path_under(path: str, root: str, platform: Optional[str] = None) -> bool:
    """True if ``path`` is ``root`` or anywhere below it (component-wise)."""
    pk, rk = path_key(path, platform), path_key(root, platform)
    if pk == rk:
        return True
    sep = sep_of(platform)
    return pk.startswith(rk if rk.endswith(sep) else rk + sep)


def dedupe_roots(paths: Iterable[str], platform: Optional[str] = None) -> list:
    """Drop duplicate roots (per case rules) and roots nested in another root,
    keeping the first spelling and the configured order."""
    out: list = []
    for p in paths:
        if not p or any(is_path_under(p, kept, platform) for kept in out):
            continue
        out = [kept for kept in out if not is_path_under(kept, p, platform)]
        out.append(p)
    return out


def _with_sep(key: str, sep: str) -> str:
    return key if key.endswith(sep) else key + sep


class Exclusions:
    """Compiled exclusion rules for one engine configuration.

    * ``names``: exact names anywhere (built-in + OS + user names), as keys.
    * user globs (``*.tmp``) over single names; user ``a/b`` sequences over a run
      of consecutive components.
    * absolute dirs skipped during a walk: OS root excludes, Windows drive-root
      names, home-anchored folders (only below a root that contains them), the
      engine's own cache/data/log dirs and extra ``own_paths``.
    """

    def __init__(self, patterns: Sequence[str] = (), *, platform: Optional[str] = None,
                 home: Optional[str] = None, own_dirs: Sequence[str] = (), own_files: Sequence[str] = (),
                 include_home_library: bool = False, index_hidden: bool = True,
                 descend_packages: bool = False, builtin: bool = True):
        p = self.platform = _plat(platform)
        self.fold = folds_case(p)
        self.sep = sep_of(p)
        self.index_hidden = index_hidden
        self.descend_packages = descend_packages
        self.packages_apply = p == "darwin"
        nk = self.key
        names = set()
        if builtin:
            names |= set(COMMON_NAMES) | system_folder_names(p)
        self.globs: list = []
        self.sequences: list = []
        for raw in patterns or ():
            if not isinstance(raw, str):
                continue
            pat = raw.strip()
            if not pat:
                continue
            if "/" in pat or "\\" in pat:
                parts = tuple(nk(x) for x in pat.replace("\\", "/").split("/") if x)
                if len(parts) == 1:
                    names.add(pat.strip("/\\"))
                elif parts:
                    self.sequences.append(parts)
            elif set("*?[") & set(pat):
                self.globs.append(nk(pat))
            else:
                names.add(pat)
        self.names = frozenset(nk(n) for n in names)
        self.name_lens = frozenset(len(n) for n in self.names)
        self.raw_names = frozenset(names)
        self._glob_rx = (re.compile("|".join(fnmatch.translate(g) for g in self.globs)).match
                         if self.globs else None)
        self._seq_last: dict = {}
        for seq in self.sequences:
            self._seq_last.setdefault(seq[-1], []).append(seq[:-1])
        self.name_lens = self.name_lens | frozenset(len(x) for x in self._seq_last)
        self.trash_prefix = p != "win32"         # ".Trash-1000" on removable media
        self.drive_names = frozenset(nk(n) for n in drive_root_names(p))
        absdirs = set(root_excludes(p))
        if home:
            absdirs |= set(home_excludes(home, p, include_home_library))
        self.abs_dirs = frozenset(path_key(d, p) for d in absdirs)
        own = []
        for d in own_dirs:
            if d:
                own.append(path_key(d, p))
                try:
                    real = os.path.realpath(d)
                except (OSError, ValueError):
                    real = d
                own.append(path_key(real, p))
        self.own_dir_keys = frozenset(own)
        files = []
        for f in own_files:
            if f:
                files.append(path_key(f, p))
        self.own_file_prefixes = tuple(sorted(set(files)))
        self.own_prefixes = tuple(sorted({_with_sep(k, self.sep) for k in self.own_dir_keys})) + self.own_file_prefixes

    # ------------------------------------------------------------------ keys
    def key(self, name: str) -> str:
        return name_key(name, self.platform)

    def pkey(self, path: str) -> str:
        """Cheap path key for paths built by scandir / watchdog (already normalized)."""
        p = self.platform
        if p == "win32":
            return path.lower() if path.isascii() else path.casefold()
        if p == "darwin":
            return path.lower() if path.isascii() else unicodedata.normalize("NFC", path).casefold()
        return path

    # ------------------------------------------------------------------ per name
    def skip_name(self, name: str) -> bool:
        """Excluded by name (anywhere in the tree)."""
        k = self.key(name)
        if k in self.names:
            return True
        if self._glob_rx is not None and self._glob_rx(k):
            return True
        if self.trash_prefix and name.startswith(".Trash-"):
            return True
        return False

    def is_package(self, name: str) -> bool:
        return self.packages_apply and name.lower().endswith(PACKAGE_SUFFIXES)

    def seq_hit(self, name: str, dir_path: str, root: Optional[str] = None) -> bool:
        """A user ``a/b`` sequence ending at ``name`` with the preceding components
        in ``dir_path`` (below ``root`` when given)."""
        if not self._seq_last:
            return False
        prefixes = self._seq_last.get(self.key(name))
        if not prefixes:
            return False
        rel = dir_path
        if root is not None and is_path_under(dir_path, root, self.platform):
            rel = dir_path[len(root):]
        parts = [self.key(x) for x in rel.replace("\\", "/").split("/") if x] if self.platform == "win32" \
            else [self.key(x) for x in rel.split("/") if x]
        for pre in prefixes:
            n = len(pre)
            if n <= len(parts) and tuple(parts[len(parts) - n:]) == pre:
                return True
        return False

    @property
    def has_sequences(self) -> bool:
        return bool(self._seq_last)

    # ------------------------------------------------------------------ per dir
    def skip_dir(self, path: str, parent_is_drive_root: bool = False, name: str = "") -> bool:
        """A directory met during a walk that must not be indexed or entered."""
        k = self.pkey(path)
        if k in self.abs_dirs or k in self.own_dir_keys:
            return True
        if parent_is_drive_root and self.drive_names and self.key(name) in self.drive_names:
            return True
        return False

    def is_own(self, path: str) -> bool:
        """The engine's own dirs/files (DB, WAL, snapshot, history, lock, logs)."""
        k = self.pkey(path)
        return k in self.own_dir_keys or k.startswith(self.own_prefixes)

    def near_own(self, dir_path: str) -> bool:
        """True if entries of ``dir_path`` may be own files (the dir is an own dir,
        inside one, or the parent of an own file)."""
        if not self.own_prefixes:
            return False
        k = _with_sep(self.pkey(dir_path), self.sep)
        return any(o.startswith(k) or k.startswith(o) for o in self.own_prefixes)

    def below_root(self, root: str) -> tuple:
        """``(prefixes, exact)`` path keys of absolute excludes strictly below
        ``root`` (anchored/OS dirs only apply when a root contains them):
        ``prefixes`` end with a separator (for the subtree), ``exact`` is the
        set of the folders themselves."""
        rk = path_key(root, self.platform)
        rks = _with_sep(rk, self.sep)
        exact = {d for d in self.abs_dirs if d != rk and d.startswith(rks)}
        return tuple(sorted(_with_sep(d, self.sep) for d in exact)), frozenset(exact)
