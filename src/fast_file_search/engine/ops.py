"""Index change operations (design H.1).

One op list is applied identically by ``Store.apply`` (SQLite, one transaction) and by
``MemIndex.apply`` (in memory), which is what keeps the two from diverging. Ops are plain
positional tuples so they are cheap to build, pickle and log.

Ordering rules the producer (the writer thread) follows:

* A ``DIR`` op precedes every op that references the directory (its children's ``INS``).
* A folder's ``INS`` (its entry row, id == dir id) may come before or after its ``DIR``.
* A folder rename/move is ``REN`` of its entry followed by ``DIRPATH`` for its subtree.
* Ids come from ``Store.allocate_ids`` and increase monotonically.
"""
from __future__ import annotations

from typing import Tuple

INS = 1       # (INS, id, dir_id, name, size, mtime, ctime, flags)
UPD = 2       # (UPD, id, size, mtime, ctime, flags)
REN = 3       # (REN, id, new_dir_id, new_name)        keeps id; for folders follow with DIRPATH
DEL = 4       # (DEL, id)                               folders: cascades to the dir subtree
DIR = 5       # (DIR, dir_id, parent_id, root_id, path, mtime)   roots: parent_id 0, no entry row
DIRPATH = 6   # (DIRPATH, dir_id, new_path)             rewrites path prefix for the whole subtree
DIRMTIME = 7  # (DIRMTIME, dir_id, mtime, flags)
ROOTDEL = 8   # (ROOTDEL, root_id)                      removes root dir, subtree, roots row

OP_NAMES = {INS: "INS", UPD: "UPD", REN: "REN", DEL: "DEL", DIR: "DIR", DIRPATH: "DIRPATH",
            DIRMTIME: "DIRMTIME", ROOTDEL: "ROOTDEL"}
OP_ARITY = {INS: 8, UPD: 6, REN: 4, DEL: 2, DIR: 6, DIRPATH: 3, DIRMTIME: 4, ROOTDEL: 2}

Op = Tuple


def describe(op: Op) -> str:
    """Readable form for logs: ``INS(12, 3, 'a.txt', ...)``."""
    name = OP_NAMES.get(op[0], f"OP{op[0]}") if op else "EMPTY"
    return f"{name}{tuple(op[1:])!r}"


def validate(op: Op) -> None:
    """Raise ``ValueError`` for a malformed op (used by tests and debug builds)."""
    if not op or op[0] not in OP_ARITY:
        raise ValueError(f"unknown op {op!r}")
    if len(op) != OP_ARITY[op[0]]:
        raise ValueError(f"{OP_NAMES[op[0]]} needs {OP_ARITY[op[0]]} fields: {op!r}")
