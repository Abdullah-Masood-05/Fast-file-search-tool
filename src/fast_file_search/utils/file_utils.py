from __future__ import annotations

import fnmatch
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


def format_size(size_bytes: int) -> str:
    """Return human-readable file size."""
    if size_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.2f} {units[i]}"


def get_file_extensions(path: str | Path) -> List[str]:
    p = Path(path)
    if p.is_dir():
        return []
    parts = p.name.split('.')
    if len(parts) <= 1:
        return []
    # include leading dot for consistency
    return [f".{ext}" for ext in parts[1:]]


def is_binary_file(path: str | Path, blocksize: int = 4096) -> bool:
    """Heuristic check for binary files by searching for NUL bytes or high non-text ratio."""
    try:
        with open(path, 'rb') as f:
            chunk = f.read(blocksize)
            if not chunk:
                return False
            if b'\x00' in chunk:
                return True
            text_chars = bytearray({7,8,9,10,12,13,27} | set(range(0x20, 0x100)))
            nontext = chunk.translate(None, text_chars)
            return float(len(nontext)) / max(1, len(chunk)) > 0.30
    except Exception:
        # If unreadable, consider binary to avoid loading into text workflows
        return True


def get_file_metadata(path: str | Path) -> Dict[str, object]:
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        raise
    except Exception:
        return {}
    return {
        "path": str(p),
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "ctime": int(st.st_ctime),
        "mode": st.st_mode,
        "is_dir": p.is_dir(),
    }


def safe_file_operations(func: Callable[..., object], retries: int = 3, delay: float = 0.1, *args, **kwargs):
    """Call func with retries on transient IO errors."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except (OSError, IOError) as exc:
            last_exc = exc
            if attempt == retries:
                raise
            time.sleep(delay * attempt)
    if last_exc:
        raise last_exc


@dataclass
class FileHash:
    """Quick change detection using stat fields and optional content sampling."""

    size: int
    mtime: int
    digest: Optional[str] = None

    @classmethod
    def from_path(cls, path: str | Path, sample: bool = False) -> "FileHash":
        p = Path(path)
        try:
            st = p.stat()
        except Exception:
            return cls(size=0, mtime=0, digest=None)
        size = st.st_size
        mtime = int(st.st_mtime)
        digest = None
        if sample and p.is_file():
            try:
                h = hashlib.sha1()
                with open(p, 'rb') as f:
                    chunk = f.read(8192)
                    if chunk:
                        h.update(chunk)
                        f.seek(max(0, size - 8192))
                        h.update(f.read(8192))
                digest = h.hexdigest()
            except Exception:
                digest = None
        return cls(size=size, mtime=mtime, digest=digest)

    def changed(self, other: "FileHash") -> bool:
        if not other:
            return True
        if self.size != other.size or self.mtime != other.mtime:
            return True
        if self.digest and other.digest and self.digest != other.digest:
            return True
        return False


_IS_CASE_INSENSITIVE = os.name == "nt" or sys.platform == "darwin"
_GLOB_CHARS = set("*?[")


def _fold(text: str) -> str:
    return text.casefold() if _IS_CASE_INSENSITIVE else text


class ExcludeMatcher:
    """Match exclude patterns against path *components*, never raw substrings.

    * ``".git"`` excludes any file/folder whose name is exactly ``.git``
      (but not ``.github`` or ``my.git.notes``).
    * Patterns containing ``* ? [`` are treated as ``fnmatch`` globs applied
      to single names (``*.tmp``).
    * Patterns containing a path separator (``build/output``) match a run of
      consecutive components anywhere in the path.
    """

    def __init__(self, patterns: Optional[Iterable[str]] = None):
        self.names: set = set()
        self.globs: List[str] = []
        self.sequences: List[tuple] = []
        for raw in patterns or []:
            if not isinstance(raw, str):
                continue
            pat = raw.strip()
            if not pat:
                continue
            if "/" in pat or "\\" in pat:
                parts = tuple(_fold(p) for p in pat.replace("\\", "/").split("/") if p)
                if parts:
                    self.sequences.append(parts)
            elif _GLOB_CHARS & set(pat):
                self.globs.append(_fold(pat))
            else:
                self.names.add(_fold(pat))

    def __bool__(self) -> bool:
        return bool(self.names or self.globs or self.sequences)

    def matches_name(self, name: str) -> bool:
        folded = _fold(name)
        if folded in self.names:
            return True
        return any(fnmatch.fnmatchcase(folded, g) for g in self.globs)

    def matches_path(self, path: str | Path) -> bool:
        parts = [p for p in str(path).replace("\\", "/").split("/") if p]
        for part in parts:
            if self.matches_name(part):
                return True
        if self.sequences:
            folded = [_fold(p) for p in parts]
            for seq in self.sequences:
                n = len(seq)
                for i in range(len(folded) - n + 1):
                    if tuple(folded[i:i + n]) == seq:
                        return True
        return False


def build_file_info(path: str | Path, st: Optional[os.stat_result] = None,
                    is_dir: Optional[bool] = None) -> Dict[str, object]:
    """Metadata dict in the shape expected by ``PersistentIndex.add_file``."""
    p = Path(path)
    if st is None:
        st = p.stat()
    if is_dir is None:
        is_dir = p.is_dir()
    ext = p.suffix.lower()
    return {
        "path": str(p),
        "name": p.name,
        "extension": ext or ("Folder" if is_dir else "File"),
        "size": st.st_size,
        "modified": int(st.st_mtime),
        "created": int(st.st_ctime),
        "is_folder": bool(is_dir),
        "parent_path": str(p.parent),
    }


# Convenience helpers
def compute_quick_hash(path: str | Path) -> FileHash:
    return FileHash.from_path(path, sample=False)
