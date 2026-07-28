from __future__ import annotations

import hashlib
import os
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


# Convenience helpers
def compute_quick_hash(path: str | Path) -> FileHash:
    return FileHash.from_path(path, sample=False)
