"""Marshal snapshot of the in-memory index (design A.4, D7).

Loading a snapshot (about 0.15 s at 316k) replaces a rebuild from SQLite (about 1 s) when the
snapshot was written at the generation the DB is at now. The file holds two marshal objects:
a small header ``(magic, layout, FOLD_VERSION, python (major, minor), db_generation, n_slots)``
checked before the payload is read, and the payload (all columns, an end marker last).

Writes go to a temp file that is fsynced and then ``os.replace``d into place, so a crash
leaves either the old or the new snapshot. Any problem on load deletes the file and returns
None; the caller then rebuilds from SQLite.
"""
from __future__ import annotations

import logging
import marshal
import os
import sys
import time
from array import array
from collections import Counter
from itertools import repeat
from operator import add, and_, lshift, or_
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Type

from .textfold import FOLD_VERSION

if TYPE_CHECKING:  # pragma: no cover
    from .memindex import MemIndex

log = logging.getLogger(__name__)

SNAPSHOT_NAME = "index-v5.snap"
MAGIC = "ffs-memindex"
LAYOUT = 3
_END = "ffs-end"
_DAY = 86_400

_ARRAYS = ("ids", "size", "mtime", "ctime", "bmask", "dir_ids", "dir_parent", "dir_root", "dir_depth",
           "dir_mtime")
_BYTES = ("flags", "live", "dir_live", "dir_hidden", "dir_flags")
# Lists of small shared ints, stored as array('I') bytes and re-shared through one int table.
_INT_LISTS = ("dslot", "ext")
_PLAIN = ("_ini_done", "_runs", "ext_names", "dir_path", "dir_key", "_sorted_len", "_id_overlay", "dead",
          "dir_slot_of", "dir_children", "dir_rank_base", "root_slot", "dirs_version", "_last_compact")
_NAME_SEP = "\x00"      # never part of a file name


def header_for(generation: int, n_slots: int) -> tuple:
    return (MAGIC, LAYOUT, FOLD_VERSION, tuple(sys.version_info[:2]), int(generation), int(n_slots))


def save(index: "MemIndex", path: Path) -> None:
    """Write ``index`` (caller holds the index lock and has built the blobs).

    Per-slot data is stored as raw bytes or joined strings, so loading is a few C-level
    ``frombytes`` / ``split`` calls instead of unmarshalling hundreds of thousands of objects."""
    path = Path(path)
    payload = {name: array_bytes(getattr(index, name)) for name in _ARRAYS}
    payload.update({name: bytes(getattr(index, name)) for name in _BYTES})
    payload.update({name: array("I", getattr(index, name)).tobytes() for name in _INT_LISTS})
    payload["srank"] = array("q", index.srank).tobytes()
    payload.update({name: getattr(index, name) for name in _PLAIN})
    names = index.names
    payload["names"] = names if any(_NAME_SEP in x for x in names) else _NAME_SEP.join(names)
    done = index._ini_done
    inits = index.inits[:done]
    payload["inits"] = inits if any("\n" in x for x in inits) else "\n".join(inits)
    payload["_ext_id"] = index._ext_id
    payload["_rc"] = index._rc
    payload["_ext_hist"] = dict(index._ext_hist)
    payload["segs"] = [(sg.lo, sg.hi, sg.blob, sg.offs.tobytes()) for sg in index._segs]
    # keys are rebuilt from the segment blobs; store them only if a key contains a newline
    payload["keys"] = index.keys if any("\n" in k for k in index.keys) else None
    payload["saved_at"] = index.clock()
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(tmp, "wb") as fh:
            marshal.dump(header_for(index.generation, index.n), fh)
            marshal.dump((payload, _END), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def array_bytes(col) -> bytes:
    if not isinstance(col, array):
        raise TypeError(f"snapshot column must be an array, got {type(col).__name__}")
    return col.tobytes()


def _discard(path: Path, why: str) -> None:
    log.info("snapshot %s not used: %s", path.name, why)
    try:
        os.remove(path)
    except OSError:
        pass


def load(cls: Type["MemIndex"], path: Path, expect_generation: int) -> Optional["MemIndex"]:
    """A ``MemIndex`` from ``path`` if its header matches and it was written at
    ``expect_generation``; otherwise None (and the file is deleted)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        why = None
        with open(path, "rb") as fh:   # (Windows: a file cannot be deleted while open)
            header = marshal.load(fh)
            if not isinstance(header, tuple) or len(header) != 6 or header[0] != MAGIC:
                why = "not a snapshot"
            else:
                want = header_for(expect_generation, header[5])
                if header != want:
                    why = f"header {header[1:5]} != {want[1:5]}"
                else:
                    body = marshal.load(fh)
        if why is not None:
            _discard(path, why)
            return None
        payload, end = body
        if end != _END:
            raise ValueError("truncated")
        return _restore(cls, payload, expect_generation, header[5])
    except Exception as exc:  # any damage: rebuild from SQLite
        _discard(path, f"unreadable ({exc.__class__.__name__}: {exc})")
        return None


def _restore(cls: Type["MemIndex"], payload: dict, generation: int, n_slots: int) -> "MemIndex":
    from .memindex import _Seg
    m = cls()
    for name in _ARRAYS:
        a = array(getattr(m, name).typecode)
        a.frombytes(payload[name])
        setattr(m, name, a)
    for name in _BYTES:
        setattr(m, name, bytearray(payload[name]))
    for name in _INT_LISTS:
        a = array("I")
        a.frombytes(payload[name])
        table = list(range(max(a) + 1)) if len(a) else []
        setattr(m, name, list(map(table.__getitem__, a)))
    sr = array("q")
    sr.frombytes(payload["srank"])
    m.srank = sr.tolist()
    for name in _PLAIN:
        setattr(m, name, payload[name])
    names = payload["names"]
    m.names = names if isinstance(names, list) else (names.split(_NAME_SEP) if n_slots else [])
    inits = payload["inits"]
    done = m._ini_done
    if not isinstance(inits, list):
        inits = inits.split("\n") if done else []
    m.inits = inits + [None] * (n_slots - done)
    m._ext_id = payload["_ext_id"]
    m._rc = payload["_rc"]
    m._ext_hist = Counter(payload["_ext_hist"])
    segs = []
    keys = payload["keys"]
    rebuilt: list = []
    for lo, hi, blob, offs in payload["segs"]:
        sg = _Seg(lo)
        sg.hi = hi
        sg.blob = blob
        sg.offs = array("I")
        sg.offs.frombytes(offs)
        sg.dirty = False
        segs.append(sg)
        if keys is None:
            part = blob.split("\n")
            part.pop()
            rebuilt.extend(part)
    m._segs = segs
    m.keys = keys if keys is not None else rebuilt
    if not (len(m.ids) == len(m.keys) == len(m.names) == len(m.srank) == len(m.dslot) == len(m.inits)
            == n_slots):
        raise ValueError("column lengths disagree")
    m.generation = generation
    if m.clock() - float(payload.get("saved_at", 0)) > 3600:
        refresh_age_buckets(m)
    return m


def refresh_age_buckets(m: "MemIndex") -> None:
    """Recompute the age bits of the static rank (A.7) with C-level passes."""
    from .memindex import _AGE_SHIFT
    now = m.clock()
    mt = m.mtime
    t7, t30, t365 = now - 7 * _DAY, now - 30 * _DAY, now - 365 * _DAY
    buckets = map(add, map(add, map(t7.__gt__, mt), map(t30.__gt__, mt)), map(t365.__gt__, mt))
    clear = ~(3 << _AGE_SHIFT)
    m.srank = list(map(or_, map(and_, m.srank, repeat(clear)), map(lshift, buckets, repeat(_AGE_SHIFT))))


def snapshot_age(path: Path) -> Optional[float]:
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None
