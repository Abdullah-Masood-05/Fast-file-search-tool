"""In-memory column index that serves every interactive query (design A.4-A.9, E.1).

Layout: one *slot* per entry, appended in id order (ids are allocated monotonically, so slot
order is id order). Per-slot columns are Python lists of str (raw names, folded keys,
initials) and typed ``array``/``bytearray`` columns. Folded keys are also joined per
*segment* of <= 131,072 slots into a ``"\\n"``-separated blob with an offsets array, so a
selective term is found with C-level ``str.find`` + ``bisect`` and a common one with
``map(contains)``. Directories (about 6% of entries) have their own small columns.

Every filter is a cached C-level mask (``bytearray`` of 0/1 per slot) that is extended, not
rebuilt, when only appends happened, and patched or dropped on in-place changes. All filters
run before ranking and before the limit, so ``SearchResult.total`` is exact.

Updates: ``INS`` appends, ``UPD`` and ``REN`` change a slot in place (a rename keeps the slot,
so slot order stays id order), ``DEL`` tombstones (``live[s] = 0``) and folder deletes
tombstone the subtree. Compaction drops tombstones and refreshes the static rank.

Concurrency: one ``threading.Lock``; a search holds it for its whole duration (<= 60 ms at
300k), so ``apply`` waits for at most one search, and vice versa.
"""
from __future__ import annotations

import bisect
import heapq
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from array import array
from collections import Counter, OrderedDict
from itertools import accumulate, compress, islice, repeat
from operator import add, and_, contains, is_, itemgetter, lshift, lt, methodcaller, mul, ne, not_, or_, truth
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from .. import kinds as _kinds
from .ops import DEL, DIR, DIRMTIME, DIRPATH, INS, REN, ROOTDEL, UPD
from .protocol import (FOLDER, HIDDEN, PACKAGE, UNREADABLE, FilterState, ResultRow, SearchRequest,
                       SearchResult, Suggestion)
from .query import Atom, ParsedQuery, ceil_time, glob_regex, literal_chunks, parse
from .textfold import boundaries, fold, initials_spans, match_spans, nfc, regex_spans

log = logging.getLogger(__name__)

SEGMENT = 131_072                 # slots per key-blob segment
WALK_ABOVE = 20_000               # filters-only results larger than this walk the global rank order
FIND_BELOW = 12                   # find loop when a term's estimated hits < n / 12, else map(contains)
MASK_CACHE = 32                   # cached masks (LRU)
PATCH_LIMIT = 256                 # in-place changes patched into cached masks; more -> drop
TIER2_EXACT = 10_000              # tier 2 decided over all non-prefix hits up to this many
TIER2_WINDOW = 2_000              # ... otherwise over this many best-static-rank hits
ABBREV_BELOW = 50                 # abbreviation tier only when tiers 0-3 have fewer hits
SUGGEST_FROM = 2_000              # name suggestions come from this many top-ranked hits
HEAP_PAGES = 5_000                # paging below this offset+limit uses heaps, beyond a full sort
FULL_SORT_BELOW = 20_000          # non-relevance sorts of fewer hits are simply fully sorted
_VAL_OFFSET = 1 << 40             # keeps sort values (sizes, mtimes, ranks; -1 for folders) positive
REGEX_TIME_CAP = 2.0
HISTORY_BUDGET = 0.015
DID_YOU_MEAN_BUDGET = 0.030
COMPACT_RATIO = 0.10
COMPACT_EVERY = 86_400.0

_DAY = 86_400
_WIN = os.name == "nt"
_MAC = sys.platform == "darwin"
_SEPS = "\\/" if _WIN else "/"
# flag byte -> 0/1 tables for bytearray.translate (C-level masks over the flags column)
_T_FOLDER = bytes(1 if f & FOLDER else 0 for f in range(256))
_T_FILE = bytes(0 if f & FOLDER else 1 for f in range(256))
_T_HIDDEN = bytes(1 if f & HIDDEN else 0 for f in range(256))
_T_NOEXT = bytes(1 if (f & FOLDER and not f & PACKAGE) else 0 for f in range(256))
_T_NOT = bytes([1] + [0] * 255)       # 0/1 mask inversion
_NAME_KINDS = _kinds.NAME_KINDS


class RootCounts(NamedTuple):
    entries: int
    folders: int
    bytes: int
    denied_dirs: int


class IndexCounters(NamedTuple):
    total_entries: int
    total_folders: int
    total_bytes: int
    roots: dict            # root_id -> RootCounts
    top_exts: tuple = ()   # ((ext, files), ...) the 20 most common file extensions


def path_key(p: str) -> str:
    """Comparison key for absolute paths under the platform's rules (case-insensitive and
    either separator on Windows; NFC + case-insensitive on macOS)."""
    if _WIN:
        p = p.replace("/", "\\")
        k = p.rstrip("\\") or p
        return k.casefold()
    k = p.rstrip("/")
    if _MAC:
        if not k.isascii():
            k = unicodedata.normalize("NFC", k)
        k = k.casefold()
    return k


def _key_sep(k: str) -> str:
    return "\\" if _WIN else "/"


def _join(dir_path: str, name: str) -> str:
    if dir_path.endswith(("/", "\\")):
        return dir_path + name
    if "\\" in dir_path and (_WIN or "/" not in dir_path):
        return dir_path + "\\" + name
    if "/" in dir_path:
        return dir_path + "/" + name
    return dir_path + os.sep + name


# Static rank field layout (A.7 order: hidden, depth, age, name length, file-after-folder). The
# in-memory ``srank`` column stores ``static_rank(...) << SLOT_BITS | slot`` so that plain integer
# comparison gives the (static rank, slot) order and heaps/sorts need no key function.
_R_HIDDEN, _R_DEPTH, _R_AGE, _R_LEN = 24, 19, 17, 1
SLOT_BITS = 26
SLOT_MASK = (1 << SLOT_BITS) - 1
_AGE_SHIFT = _R_AGE + SLOT_BITS
_RANK_FLAGS = [(((1 << _R_HIDDEN) if f & HIDDEN else 0) | (0 if f & FOLDER else 1)) << SLOT_BITS
               for f in range(256)]
_RANK_DOT = 1 << (_R_HIDDEN + SLOT_BITS)
_RANK_LEN = _R_LEN + SLOT_BITS
_RANK_AGE = [(3 - c) << (_R_AGE + SLOT_BITS) for c in range(4)]   # indexed by thresholds passed


def static_rank(hidden: int, depth: int, mtime: int, name_len: int, is_folder: bool, now: float) -> int:
    """Static rank (smaller is better), A.7. All weights live here so they are tuned together:
    ``hidden_penalty | min(depth, 31) | age_bucket | min(len(name), 0xFFFF) | (0 folder, 1 file)``,
    most significant first; age buckets are <= 7 days, <= 30 days, <= 1 year, older."""
    age = now - mtime
    bucket = 0 if age <= 7 * _DAY else 1 if age <= 30 * _DAY else 2 if age <= 365 * _DAY else 3
    return ((1 if hidden else 0) << _R_HIDDEN) | (min(depth, 31) << _R_DEPTH) | (bucket << _R_AGE) | \
        (min(name_len, 0xFFFF) << _R_LEN) | (0 if is_folder else 1)


def _fold_all(names: Iterable[str]) -> List[str]:
    """Folded keys; a name that is already its own key shares the str object (memory)."""
    out: List[str] = []
    ap = out.append
    for n in names:
        if n.isascii():
            k = n.lower()
            ap(n if k == n else k)
        else:
            ap(fold(n))
    return out


def _ext_of(key: str, flags: int) -> str:
    if flags & FOLDER and not flags & PACKAGE:
        return ""
    return _kinds.ext_of_key(key)


class _Seg:
    __slots__ = ("lo", "hi", "blob", "offs", "dirty", "iblob", "ioffs", "idirty")

    def __init__(self, lo: int):
        self.lo = lo
        self.hi = lo
        self.blob = ""
        self.offs = array("I")
        self.dirty = True
        self.iblob = ""
        self.ioffs = array("I")
        self.idirty = True


class _Mask:
    __slots__ = ("data", "deps", "comp")

    def __init__(self, comp: tuple, data: bytearray, deps: frozenset):
        self.comp = comp
        self.data = data
        self.deps = deps


class _State:
    """Everything needed to page through one search result (A.9)."""
    __slots__ = ("gen", "epoch", "pq", "sort", "hits", "t4", "total", "rel", "full", "member", "walk", "hitset",
                 "vals", "spans_terms", "rx", "case", "abbrev", "req_key", "generation", "root_counts",
                 "error", "elapsed")

    def __init__(self):
        self.rel = None
        self.member = None   # per-slot 0/1 membership of the result (filters-only results)
        self.walk = None
        self.hitset = None
        self.full = None
        self.vals = None
        self.t4: List[int] = []
        self.rx = None
        self.abbrev = None
        self.root_counts: tuple = ()
        self.error = None


class MemIndex:
    """See the module docstring. Public API is frozen (design H.1)."""

    clock: Callable[[], float] = staticmethod(time.time)  # overridable for tests

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.generation = 0
        self.partial = False                 # set by the service while a first scan runs
        self.epoch = 0                       # bumped by compaction (slots renumbered)
        self.stats = Counter()               # instrumentation: mask_build, mask_extend, ...
        self._last_compact = self.clock()
        # ---- per-slot columns
        self.ids = array("q")
        self.names: List[str] = []
        self.keys: List[str] = []
        self.inits: List[Optional[str]] = []      # initials, computed lazily (None = not yet)
        self.dslot: List[int] = []                 # dir slot (list: fastest for map lookups)
        self.size = array("q")
        self.mtime = array("q")
        self.ctime = array("q")
        self.flags = bytearray()
        self.ext: List[int] = []                   # ext id
        self.bmask = array("Q")                    # word-start bits, lazily (0 = not yet computed)
        self.srank: List[int] = []                 # static_rank << SLOT_BITS | slot
        self.live = bytearray()
        self._cs_names: Optional[List[str]] = None   # NFC names for case: queries (lazy)
        self._sorted_len = 0                          # prefix of ids that is ascending
        self._id_overlay: Dict[int, int] = {}         # ids appended out of order -> slot
        self.dead = 0
        self._segs: List[_Seg] = []
        self._bound_cache: Dict[str, Tuple[int, str]] = {}
        self._ini_done = 0                         # slots [0, _ini_done) have initials
        self._runs: Optional[List[Tuple[int, int]]] = []   # (first slot, root id) runs
        # ---- extensions
        self.ext_names: List[str] = [""]
        self._ext_id: Dict[str, int] = {"": 0}
        self._ext_rank: Optional[array] = None
        # ---- directories
        self.dir_ids = array("q")
        self.dir_slot_of: Dict[int, int] = {}
        self.dir_path: List[str] = []
        self.dir_key: List[str] = []         # folded "rootname/rel/path"
        self.dir_parent = array("q")         # dir slot of the parent, -1 for roots
        self.dir_root = array("q")           # root id
        self.dir_depth = array("H")
        self.dir_live = bytearray()
        self.dir_hidden = bytearray()
        self.dir_mtime = array("q")
        self.dir_flags = bytearray()
        self.dir_rank_base: List[int] = []   # dir part of srank: (hidden | depth) bits, shifted
        self.dir_children: Dict[int, List[int]] = {}
        self.root_slot: Dict[int, int] = {}  # root id -> dir slot
        self.dirs_version = 0
        self._dir_cs: Optional[List[str]] = None
        self._dir_by_key: Optional[Dict[str, int]] = None
        self._dir_rank: Optional[array] = None
        self._dir_pkeys: Optional[List[str]] = None
        self._dir_masks: Dict[tuple, bytearray] = {}
        self._moved_dirs: Set[int] = set()   # dirs whose depth/hidden changed in this batch
        # ---- counters (B.4)
        self._rc: Dict[int, List[int]] = {}  # root id -> [entries, folders, bytes, denied]
        self._ext_hist = Counter()           # ext id -> live files
        # ---- caches
        self._masks: "OrderedDict[tuple, _Mask]" = OrderedDict()
        self._state: Optional[_State] = None
        self._hist_cache: Dict[tuple, bool] = {}
        self._gorder: List[int] = []
        self._sort_orders: Dict[tuple, tuple] = {}
        self._gorder_key: Optional[tuple] = None

    # ================================================================== construction
    @classmethod
    def build(cls, store) -> "MemIndex":
        """Build from SQLite (``Store.iter_load``)."""
        m = cls()
        for chunk in store.iter_load():
            if chunk.dirs:
                m._load_dirs(chunk.dirs)
            if chunk.roots:
                for r in chunk.roots:
                    m._rc.setdefault(r.id, [0, 0, 0, 0])
            if chunk.entries:
                m._append(chunk.entries)
        m.generation = store.generation
        return m

    @classmethod
    def load_snapshot(cls, path: Path, expect_generation: int) -> "MemIndex | None":
        from . import snapshot
        return snapshot.load(cls, Path(path), expect_generation)

    def save_snapshot(self, path: Path) -> None:
        from . import snapshot
        with self._lock:
            self._ensure_blobs()
            snapshot.save(self, Path(path))

    # ================================================================== slot lookup
    def _slot(self, entry_id: int) -> Optional[int]:
        s = self._id_overlay.get(entry_id) if self._id_overlay else None
        if s is not None:
            return s
        i = bisect.bisect_left(self.ids, entry_id, 0, self._sorted_len)
        if i < self._sorted_len and self.ids[i] == entry_id:
            return i
        return None

    def _live_slot(self, entry_id: int) -> Optional[int]:
        s = self._slot(entry_id)
        return s if s is not None and self.live[s] else None

    # ================================================================== directories
    def _new_dir_slot(self, did: int) -> int:
        d = len(self.dir_ids)
        self.dir_ids.append(did)
        self.dir_slot_of[did] = d
        self.dir_path.append("")
        self.dir_key.append("")
        self.dir_parent.append(-1)
        self.dir_root.append(0)
        self.dir_depth.append(0)
        self.dir_live.append(1)
        self.dir_hidden.append(0)
        self.dir_mtime.append(0)
        self.dir_flags.append(0)
        self.dir_rank_base.append(0)
        return d

    def _load_dirs(self, rows: Sequence[tuple]) -> None:
        """Bulk add ``(id, parent_id, root_id, path, mtime, flags)`` rows (any order)."""
        for did, parent, root, path, mt, fl in rows:
            d = self.dir_slot_of.get(did)
            if d is None:
                d = self._new_dir_slot(did)
            self.dir_path[d] = path
            self.dir_root[d] = root
            self.dir_mtime[d] = mt
            if fl is not None:
                self.dir_flags[d] = fl & 0xFF
            self.dir_live[d] = 1
            if parent == 0:
                self.root_slot[root] = d
                self._rc.setdefault(root, [0, 0, 0, 0])
        for did, parent, root, path, mt, fl in rows:
            d = self.dir_slot_of[did]
            p = self.dir_slot_of.get(parent, -1) if parent else -1
            self._link(d, p)
            if fl is not None and fl & UNREADABLE:
                self._rc.setdefault(root, [0, 0, 0, 0])[3] += 1
        self._derive_dirs(self._topo(rows))
        self._dirs_changed(appended_only=True)

    def _topo(self, rows: Sequence[tuple]) -> List[int]:
        """Dir slots of ``rows`` in parent-before-child order."""
        return self._topo_slots({self.dir_slot_of[r[0]] for r in rows})

    def _topo_slots(self, want: Set[int]) -> List[int]:
        order: List[int] = []
        seen: Set[int] = set()
        for d in sorted(want):
            chain = []
            x = d
            while x >= 0 and x not in seen and x in want:
                chain.append(x)
                seen.add(x)
                x = self.dir_parent[x]
            order.extend(reversed(chain))
        return order

    def _link(self, d: int, p: int) -> None:
        old = self.dir_parent[d]
        if old == p:
            return
        if old >= 0:
            kids = self.dir_children.get(old)
            if kids and d in kids:
                kids.remove(d)
        self.dir_parent[d] = p
        if p >= 0:
            self.dir_children.setdefault(p, []).append(d)

    def _derive_dirs(self, order: Iterable[int]) -> None:
        """Recompute key / depth / hidden / root for dirs (parents first)."""
        for d in order:
            p = self.dir_parent[d]
            path = self.dir_path[d]
            if p < 0:
                base = path.rstrip(_SEPS) or path
                name = re.split(r"[\\/]" if _WIN else "/", base)[-1] or base
                self.dir_key[d] = fold(name)
                self.dir_depth[d] = 0
                self.dir_hidden[d] = 0
                self.dir_rank_base[d] = 0
                continue
            ppath = self.dir_path[p]
            if path.startswith(ppath):
                bn = path[len(ppath):].lstrip(_SEPS)
            else:
                bn = re.split(r"[\\/]" if _WIN else "/", path.rstrip(_SEPS))[-1]
            fb = fold(bn)
            self.dir_key[d] = self.dir_key[p] + "/" + (fb.replace("\\", "/") if _WIN else fb)
            self.dir_depth[d] = min(self.dir_depth[p] + 1, 0xFFFF)
            self.dir_root[d] = self.dir_root[p]
            # "under a hidden dir" = some folder below the root has a dot name (A.7)
            self.dir_hidden[d] = 1 if (self.dir_hidden[p] or bn.startswith(".")) else 0
            self.dir_rank_base[d] = ((self.dir_hidden[d] << _R_HIDDEN) | (min(self.dir_depth[d], 31) << _R_DEPTH)) \
                << SLOT_BITS

    def _subtree_dirs(self, d: int) -> List[int]:
        out = [d]
        i = 0
        while i < len(out):
            out.extend(self.dir_children.get(out[i], ()))
            i += 1
        return out

    def _dirs_changed(self, appended_only: bool) -> None:
        if not appended_only:
            self.dirs_version += 1
            self._dir_masks.clear()
            self._dir_cs = None
            for key in [k for k, m in self._masks.items() if "dirs" in m.deps or k[0] == "ALL"]:
                del self._masks[key]
        else:
            self._dir_pkeys = None
        self._dir_by_key = None
        self._dir_rank = None

    def _dir_slot_for(self, did: int) -> int:
        d = self.dir_slot_of.get(did)
        if d is None:   # entry for a dir we were not told about: placeholder, fixed by its DIR op
            d = self._new_dir_slot(did)
            self.dir_live[d] = 1
        return d

    # ================================================================== appends
    def _append(self, rows: Sequence[tuple], ops: bool = False) -> None:
        """Append ``(id, dir_id, name, size, mtime, ctime, flags)`` rows (one batch), or the
        same as ``INS`` op tuples when ``ops`` is true.

        Only C-level passes (``map``/``compress``/``translate``) plus a Python loop over the
        non-ASCII names; word boundaries and initials are computed later, on first need."""
        if not rows:
            return
        n0 = len(self.ids)
        k = len(rows)
        if ops:
            _, ids, dirs, names, sizes, mts, cts, fls = zip(*rows)
        else:
            ids, dirs, names, sizes, mts, cts, fls = zip(*rows)
        # ids: keep the ascending prefix for bisect; out-of-order ids go to the overlay.
        if self._sorted_len == n0 and (n0 == 0 or ids[0] > self.ids[-1]) and all(map(lt, ids, ids[1:])):
            self._sorted_len = n0 + k
        else:
            for j, i in enumerate(ids):
                s = n0 + j
                if self._sorted_len == s and (s == 0 or i > self.ids[s - 1]):
                    self._sorted_len += 1
                else:
                    self._id_overlay[i] = s
        self.ids.extend(ids)
        try:
            ds = list(map(self.dir_slot_of.__getitem__, dirs))
        except KeyError:
            ds = list(map(self._dir_slot_for, dirs))
        self.dslot.extend(ds)
        self.names.extend(names)
        keys = list(map(str.lower, names))
        for j in compress(range(k), map(not_, map(str.isascii, names))):
            keys[j] = fold(names[j])
        self.keys.extend(keys)
        self.size.extend(sizes)
        self.mtime.extend(mts)
        self.ctime.extend(cts)
        fb = bytes(map(and_, fls, repeat(0xFF)))
        self.flags.extend(fb)
        self.live.extend(b"\x01" * k)
        if self._cs_names is not None:
            self._cs_names.extend(map(nfc, names))
        # extension ids: text after the last dot when a dot follows the first character
        parts = list(map(methodcaller("rpartition", "."), keys))
        exts = list(map(itemgetter(2), parts))
        for j in compress(range(k), map(not_, map(itemgetter(0), parts))):
            exts[j] = ""
        for j in compress(range(k), fb.translate(_T_NOEXT)):
            exts[j] = ""
        table = self._ext_id
        for e in set(exts).difference(table):
            table[e] = len(self.ext_names)
            self.ext_names.append(e)
            self._ext_rank = None
        eids = list(map(table.__getitem__, exts))
        self.ext.extend(eids)
        # lazily computed columns
        self.bmask.frombytes(bytes(8 * k))
        self.inits.extend(repeat(None, k))
        # static rank (C-level map chains; see static_rank for the layout)
        self.srank.extend(self._static_ranks(ds, keys, fb, mts, n0))
        # counters
        rc = self._rc
        dr = self.dir_root
        roots = list(map(dr.__getitem__, ds))
        notdir = fb.translate(_T_FILE)
        if roots[0] == roots[-1] and roots.count(roots[0]) == k:      # the usual case: one root
            r = rc.setdefault(roots[0], [0, 0, 0, 0])
            r[0] += k
            r[1] += k - sum(notdir)
            r[2] += sum(compress(sizes, notdir))
        else:
            for r, c in Counter(roots).items():
                rc.setdefault(r, [0, 0, 0, 0])[0] += c
            for r, c in Counter(compress(roots, fb.translate(_T_FOLDER))).items():
                rc[r][1] += c
            for r, sz in zip(compress(roots, notdir), compress(sizes, notdir)):
                rc[r][2] += sz
        self._ext_hist.update(compress(eids, notdir))
        # root runs (contiguous slot ranges of one root) for O(runs) root counts
        runs = self._runs
        if runs is not None:
            if not runs or runs[-1][1] != roots[0]:
                runs.append((n0, roots[0]))
            for j in compress(range(1, k), map(ne, roots[1:], roots)):
                runs.append((n0 + j, roots[j]))
        # segments
        self._grow_segments(len(self.ids))

    def _static_ranks(self, ds, keys, fb, mts, n0: int):
        """srank values for a batch (same result as ``static_rank(...) << SLOT_BITS | slot``):
        the dir part comes precomputed per dir, the rest from table lookups and C-level maps.
        (Names are <= 255 characters on every supported file system, so len < 0xFFFF.)"""
        now = self.clock()
        thr = [now - 365 * _DAY, now - 30 * _DAY, now - 7 * _DAY]   # ascending
        v = map(or_, map(self.dir_rank_base.__getitem__, ds), map(_RANK_FLAGS.__getitem__, fb))
        v = map(or_, v, map(_RANK_AGE.__getitem__, map(bisect.bisect_right, repeat(thr), mts)))
        v = map(or_, v, map(lshift, map(len, keys), repeat(_RANK_LEN)))
        v = map(or_, v, map(mul, map(methodcaller("startswith", "."), keys), repeat(_RANK_DOT)))
        return map(or_, v, range(n0, n0 + len(keys)))

    def _bounds(self, s: int) -> int:
        """Compute (and store) word-start bits and initials of slot ``s``; returns the bits."""
        nm = self.names[s]
        cache = self._bound_cache
        b = cache.get(nm)
        if b is None:
            if len(cache) > 200_000:
                cache.clear()
            b = cache[nm] = boundaries(nm, self.keys[s])
        bits = b[0] | 1
        self.bmask[s] = bits
        self.inits[s] = b[1]
        return bits

    def warm(self, budget: float = 0.05) -> bool:
        """Compute pending initials / word boundaries for up to ``budget`` seconds (call from an
        idle thread; searches otherwise compute them on first need). True when nothing is left."""
        with self._lock:
            return self._warm_locked(time.perf_counter() + budget)

    def _warm_locked(self, deadline: Optional[float]) -> bool:
        inits = self.inits
        n = len(inits)
        s = self._ini_done
        while s < n:
            if inits[s] is None:
                self._bounds(s)
            s += 1
            if deadline is not None and not s & 1023 and time.perf_counter() > deadline:
                break
        self._ini_done = s
        return s >= n

    def _grow_segments(self, n: int) -> None:
        segs = self._segs
        if not segs:
            segs.append(_Seg(0))
        while segs[-1].hi < n:
            tail = segs[-1]
            room = SEGMENT - (tail.hi - tail.lo)
            if room <= 0:
                segs.append(_Seg(tail.hi))
                continue
            tail.hi = min(n, tail.hi + room)
            tail.dirty = tail.idirty = True

    def _seg_of(self, s: int) -> _Seg:
        return self._segs[s // SEGMENT]

    def _ensure_blobs(self) -> None:
        for seg in self._segs:
            if seg.dirty:
                ks = self.keys[seg.lo:seg.hi]
                seg.blob = "\n".join(ks) + "\n" if ks else ""
                offs = array("I", accumulate(map(add, map(len, ks), repeat(1)), initial=0))
                offs.pop()
                seg.offs = offs
                seg.dirty = False

    def _ensure_iblob(self, seg: _Seg) -> None:
        if seg.idirty:
            if self._ini_done < seg.hi:
                inits = self.inits
                for s in compress(range(seg.lo, seg.hi), map(is_, inits[seg.lo:seg.hi], repeat(None))):
                    self._bounds(s)
                if self._ini_done >= seg.lo:
                    self._ini_done = seg.hi
            ks = self.inits[seg.lo:seg.hi]
            seg.iblob = "\n".join(ks) + "\n" if ks else ""
            offs = array("I", accumulate(map(add, map(len, ks), repeat(1)), initial=0))
            offs.pop()
            seg.ioffs = offs
            seg.idirty = False

    @property
    def n(self) -> int:
        return len(self.ids)

    # ================================================================== apply
    def apply(self, ops: Sequence[tuple], generation: int) -> None:
        """Apply an op batch (the same list ``Store.apply`` committed). Thread-safe."""
        with self._lock:
            self._apply_locked(ops)
            self.generation = generation

    def _apply_locked(self, ops: Sequence[tuple]) -> None:
        changed: Dict[int, Set[str]] = {}
        killed = False
        dirs_appended = False
        dirs_moved = False
        for code, run in _runs(ops):
            if code == INS:   # a run of INS and DIR ops: dirs first, then one vectorized append
                dirs = [o for o in run if o[0] == DIR]
                if dirs:
                    if any(o[1] in self.dir_slot_of for o in dirs):
                        dirs_moved = True
                    self._load_dirs([(o[1], o[2], o[3], o[4], o[5], None) for o in dirs])
                    dirs_appended = True
                self._append([o for o in run if o[0] == INS] if dirs else run, ops=True)
            elif code == UPD:
                for o in run:
                    if self._upd(o):
                        changed.setdefault("upd", set()).add(self._slot(o[1]))  # type: ignore[arg-type]
            elif code == REN:
                for o in run:
                    s = self._ren(o)
                    if s is not None:
                        changed.setdefault("ren", set()).add(s)
                        if o[1] in self.dir_slot_of:
                            dirs_moved = True
            elif code == DEL:
                killed |= self._del_run([o[1] for o in run])
            elif code == DIRPATH:
                for o in run:
                    self._dirpath(o[1], o[2])
                dirs_moved = True
            elif code == DIRMTIME:
                for o in run:
                    self._dirmtime(o[1], o[2], o[3])
            elif code == ROOTDEL:
                for o in run:
                    killed |= self._rootdel(o[1])
                dirs_moved = True
            else:
                raise ValueError(f"unknown op {run[0]!r}")
        if self._moved_dirs:
            # entries below a moved folder: depth / hidden changed, so their static rank did
            moved = self._moved_dirs
            self._moved_dirs = set()
            live = self.live
            for s in compress(range(self.n), map(moved.__contains__, self.dslot)):
                if live[s]:
                    self._rerank(s)
        if dirs_moved:
            self._runs = None          # slots may have changed roots: rebuilt on demand
            self._dirs_changed(appended_only=False)
        elif dirs_appended:
            self._dirs_changed(appended_only=True)
        self._invalidate(changed, killed)

    def _invalidate(self, changed: Dict[str, Set[int]], killed: bool) -> None:
        if not changed and not killed:
            return
        kinds = set(changed)
        slots: Set[int] = set()
        for v in changed.values():
            slots |= v
        drop = []
        for key, m in self._masks.items():
            if key[0] == "ALL":
                drop.append(key)
                continue
            if not (m.deps & kinds):
                continue
            if len(slots) > PATCH_LIMIT:
                drop.append(key)
                continue
            for s in slots:
                if s < len(m.data):
                    m.data[s] = self._build_comp(m.comp, s, s + 1)[0]
            self.stats["mask_patch"] += 1
        for key in drop:
            del self._masks[key]

    def _upd(self, o: tuple) -> bool:
        _, i, size, mtime, ctime, flags = o
        s = self._live_slot(i)
        if s is None:
            return False
        flags &= 0xFF
        old_f = self.flags[s]
        r = self.dir_root[self.dslot[s]]
        rc = self._rc.setdefault(r, [0, 0, 0, 0])
        if not old_f & FOLDER:
            rc[2] -= self.size[s]
            self._ext_hist[self.ext[s]] -= 1
        if old_f & FOLDER and not flags & FOLDER:
            rc[1] -= 1
        elif flags & FOLDER and not old_f & FOLDER:
            rc[1] += 1
        self.size[s] = size
        self.mtime[s] = mtime
        self.ctime[s] = ctime
        self.flags[s] = flags
        if (old_f ^ flags) & (FOLDER | PACKAGE):
            self.ext[s] = self._intern_ext(_ext_of(self.keys[s], flags))
        if not flags & FOLDER:
            rc[2] += size
            self._ext_hist[self.ext[s]] += 1
        self._rerank(s)
        return True

    def _intern_ext(self, e: str) -> int:
        i = self._ext_id.get(e)
        if i is None:
            i = self._ext_id[e] = len(self.ext_names)
            self.ext_names.append(e)
            self._ext_rank = None
        return i

    def _rerank(self, s: int) -> None:
        d = self.dslot[s]
        k = self.keys[s]
        f = self.flags[s]
        self.srank[s] = static_rank((f & HIDDEN) or k.startswith(".") or self.dir_hidden[d], self.dir_depth[d],
                                    self.mtime[s], len(k), f & FOLDER, self.clock()) << SLOT_BITS | s

    def _ren(self, o: tuple) -> Optional[int]:
        _, i, new_dir, new_name = o
        s = self._live_slot(i)
        if s is None:
            return None
        old_root = self.dir_root[self.dslot[s]]
        d = self._dir_slot_for(new_dir)
        new_root = self.dir_root[d]
        f = self.flags[s]
        if old_root != new_root:
            a = self._rc.setdefault(old_root, [0, 0, 0, 0])
            b = self._rc.setdefault(new_root, [0, 0, 0, 0])
            a[0] -= 1
            b[0] += 1
            if f & FOLDER:
                a[1] -= 1
                b[1] += 1
            else:
                a[2] -= self.size[s]
                b[2] += self.size[s]
        self.dslot[s] = d
        self.names[s] = new_name
        k = _fold_all([new_name])[0]
        self.keys[s] = k
        if self._cs_names is not None:
            self._cs_names[s] = nfc(new_name)
        if not f & FOLDER:
            self._ext_hist[self.ext[s]] -= 1
        self.ext[s] = self._intern_ext(_ext_of(k, f))
        if not f & FOLDER:
            self._ext_hist[self.ext[s]] += 1
        self._bounds(s)
        if old_root != new_root:
            self._runs = None
        seg = self._seg_of(s)
        seg.dirty = seg.idirty = True
        self._rerank(s)
        dd = self.dir_slot_of.get(i)
        if dd is not None:   # a folder moved: re-parent its dir (paths follow via DIRPATH)
            self._link(dd, d)
            self.dir_root[dd] = new_root
            sub = self._subtree_dirs(dd)
            self._derive_dirs(sub)
            self._moved_dirs.update(sub)
        return s

    def _kill_slots(self, slots: Iterable[int]) -> int:
        rc = self._rc
        live = self.live
        n = 0
        for s in slots:
            if not live[s]:
                continue
            live[s] = 0
            n += 1
            r = rc.setdefault(self.dir_root[self.dslot[s]], [0, 0, 0, 0])
            r[0] -= 1
            if self.flags[s] & FOLDER:
                r[1] -= 1
            else:
                r[2] -= self.size[s]
                self._ext_hist[self.ext[s]] -= 1
        self.dead += n
        return n

    def _kill_dirs(self, dslots: List[int]) -> None:
        """Tombstone dirs and every entry inside them (one C-level pass over ``dslot``)."""
        if not dslots:
            return
        dead = set(dslots)
        for d in dslots:
            if self.dir_live[d] and self.dir_flags[d] & UNREADABLE:
                self._rc.setdefault(self.dir_root[d], [0, 0, 0, 0])[3] -= 1
            self.dir_live[d] = 0
        hit = bytearray(map(dead.__contains__, self.dslot))
        self._kill_slots(compress(range(len(hit)), hit))

    def _del_run(self, ids: List[int]) -> bool:
        """DEL ops of one run: entries, then every deleted folder's subtree in one pass."""
        killed = False
        dead_dirs: List[int] = []
        for i in ids:
            s = self._live_slot(i)
            if s is not None:
                killed |= self._kill_slots((s,)) > 0
            d = self.dir_slot_of.get(i)
            if d is not None and self.dir_live[d]:
                sub = self._subtree_dirs(d)
                p = self.dir_parent[d]
                if p >= 0 and d in self.dir_children.get(p, ()):
                    self.dir_children[p].remove(d)
                for x in sub:
                    self.dir_slot_of.pop(self.dir_ids[x], None)
                dead_dirs.extend(sub)
        if dead_dirs:
            self._kill_dirs(dead_dirs)
            killed = True
        return killed

    def _rootdel(self, rid: int) -> bool:
        dslots = [d for d in range(len(self.dir_ids)) if self.dir_root[d] == rid and self.dir_live[d]]
        self._kill_dirs(dslots)
        for d in dslots:
            self.dir_slot_of.pop(self.dir_ids[d], None)
        s = self._live_slot(rid)
        if s is not None:
            self._kill_slots((s,))
        self.root_slot.pop(rid, None)
        self._rc.pop(rid, None)
        return True

    def _dirpath(self, did: int, new_path: str) -> None:
        d = self.dir_slot_of.get(did)
        if d is None:
            return
        old = self.dir_path[d]
        sub = self._subtree_dirs(d)
        for x in sub:
            p = self.dir_path[x]
            self.dir_path[x] = new_path + p[len(old):] if p.startswith(old) else p
        self._derive_dirs(sub)
        self._moved_dirs.update(sub)

    def _dirmtime(self, did: int, mtime: int, flags: int) -> None:
        d = self.dir_slot_of.get(did)
        if d is None:
            return
        old = self.dir_flags[d]
        flags &= 0xFF
        if (old ^ flags) & UNREADABLE:
            self._rc.setdefault(self.dir_root[d], [0, 0, 0, 0])[3] += 1 if flags & UNREADABLE else -1
        self.dir_mtime[d] = mtime
        self.dir_flags[d] = flags

    # ================================================================== maintenance
    def compact_if_needed(self, force: bool = False) -> bool:
        """Drop tombstones and refresh static ranks when tombstones exceed 10% or a day has
        passed since the last compaction. About 1 s at 316k; run from the writer thread."""
        with self._lock:
            now = self.clock()
            due = self.dead > COMPACT_RATIO * max(1, self.n) or now - self._last_compact > COMPACT_EVERY
            if not (force or due):
                return False
            self._compact_locked()
            return True

    def _compact_locked(self) -> None:
        live = self.live
        keep = list(compress(range(self.n), live))
        dkeep = [d for d in range(len(self.dir_ids)) if self.dir_live[d]]
        dirs = []
        for d in dkeep:
            p = self.dir_parent[d]
            parent = self.dir_ids[p] if p >= 0 else 0
            dirs.append((self.dir_ids[d], parent, self.dir_root[d], self.dir_path[d], self.dir_mtime[d],
                         self.dir_flags[d]))
        ids, dsl, names = self.ids, self.dslot, self.names
        rows = [(ids[s], self.dir_ids[dsl[s]], names[s], self.size[s], self.mtime[s], self.ctime[s], self.flags[s])
                for s in keep]
        rows.sort(key=itemgetter(0))
        roots = [r for r in self.root_slot]
        fresh = MemIndex()
        fresh.clock = self.clock  # type: ignore[assignment]
        fresh._load_dirs(dirs)
        for r in roots:
            fresh._rc.setdefault(r, [0, 0, 0, 0])
        for i in range(0, len(rows), 50_000):
            fresh._append(rows[i:i + 50_000])
        keep_attrs = ("_lock", "generation", "partial", "stats")
        for k, v in fresh.__dict__.items():
            if k not in keep_attrs:
                setattr(self, k, v)
        self.epoch += 1
        self._last_compact = self.clock()
        self.stats["compactions"] += 1

    def counters(self) -> IndexCounters:
        """O(1) per-root entries/folders/bytes/denied, totals, top-20 extensions."""
        with self._lock:
            roots = {r: RootCounts(*v) for r, v in self._rc.items() if r in self.root_slot or v[0]}
            te = sum(v.entries for v in roots.values())
            tf = sum(v.folders for v in roots.values())
            tb = sum(v.bytes for v in roots.values())
            top = heapq.nlargest(21, ((c, self.ext_names[e]) for e, c in self._ext_hist.items() if c > 0))
            exts = tuple((e, c) for c, e in top if e)[:20]
            return IndexCounters(te, tf, tb, roots, exts)

    def path_of(self, entry_id: int) -> Optional[str]:
        with self._lock:
            s = self._live_slot(entry_id)
            if s is None:
                d = self.dir_slot_of.get(entry_id)
                return self.dir_path[d] if d is not None and self.dir_live[d] else None
            return _join(self.dir_path[self.dslot[s]], self.names[s])

    def ids_for_paths(self, paths: Iterable[str]) -> List[int]:
        """Live entry ids for absolute paths (unknown paths are skipped)."""
        with self._lock:
            self._ensure_blobs()
            return [i for i in (self._id_for_path(p) for p in paths) if i is not None]

    def _dirs_by_key(self) -> Dict[str, int]:
        if self._dir_by_key is None:
            self._dir_by_key = {path_key(self.dir_path[d]): d for d in range(len(self.dir_ids)) if self.dir_live[d]}
        return self._dir_by_key

    def _id_for_path(self, path: str) -> Optional[int]:
        pk = path_key(path)
        d = self._dirs_by_key().get(pk)
        if d is not None:
            s = self._live_slot(self.dir_ids[d])
            if s is not None:
                return self.dir_ids[d]
        parent, _, name = path.rstrip(_SEPS).replace("\\", "/").rpartition("/") if _WIN else \
            path.rstrip("/").rpartition("/")
        if not name:
            return None
        pd = self._dirs_by_key().get(path_key(parent if parent else "/"))
        if pd is None:
            return None
        key = fold(name)
        for s in self._find_all(key):
            if self.dslot[s] == pd and self.live[s] and path_key(_join(self.dir_path[pd], self.names[s])) == pk:
                return self.ids[s]
        return None

    def _find_all(self, term: str) -> List[int]:
        out: List[int] = []
        for seg in self._segs:
            out.extend(self._scan_find(seg, term))
        return out

    # ================================================================== scanning
    def _estimate(self, term: str) -> int:
        segs = self._segs
        if not segs or not segs[0].blob:
            return 0
        sample = segs[0].blob[:1 << 20]
        total = sum(len(s.blob) for s in segs)
        c = sample.count(term)
        return int(c * total / max(1, len(sample)))

    def _scan_find(self, seg: _Seg, term: str) -> List[int]:
        blob, offs = seg.blob, seg.offs
        find = blob.find
        br = bisect.bisect_right
        nl = len(offs)
        base = seg.lo
        out: List[int] = []
        ap = out.append
        end = len(blob)
        pos = find(term)
        while pos >= 0:
            r = br(offs, pos) - 1
            ap(base + r)
            pos = find(term, offs[r + 1] if r + 1 < nl else end)
        return out

    def _scan_sub(self, seg: _Seg, term: str, mode: str, case: bool) -> List[int]:
        if case:
            src = self._cs()[seg.lo:seg.hi]
            return list(compress(range(seg.lo, seg.hi), map(contains, src, repeat(term))))
        if mode == "map" or "\n" in term:
            return list(compress(range(seg.lo, seg.hi), map(contains, self.keys[seg.lo:seg.hi], repeat(term))))
        return self._scan_find(seg, term)

    def _cs(self) -> List[str]:
        if self._cs_names is None:
            self._cs_names = list(map(nfc, self.names))
        return self._cs_names

    def _dir_cs_keys(self) -> List[str]:
        """Case-preserving (NFC) counterpart of ``dir_key`` for ``case:`` path matching."""
        if self._dir_cs is None or len(self._dir_cs) != len(self.dir_ids):
            out = [""] * len(self.dir_ids)
            for d in self._dir_order():
                p = self.dir_parent[d]
                path = self.dir_path[d]
                if p < 0:
                    base = path.rstrip(_SEPS) or path
                    out[d] = nfc(re.split(r"[\\/]" if _WIN else "/", base)[-1] or base)
                else:
                    pp = self.dir_path[p]
                    bn = path[len(pp):].lstrip(_SEPS) if path.startswith(pp) else \
                        re.split(r"[\\/]" if _WIN else "/", path.rstrip(_SEPS))[-1]
                    out[d] = out[p] + "/" + nfc(bn).replace("\\", "/")
            self._dir_cs = out
        return self._dir_cs

    def _dir_order(self) -> List[int]:
        roots = [d for d in range(len(self.dir_ids)) if self.dir_parent[d] < 0]
        out: List[int] = []
        for r in roots:
            out.extend(self._subtree_dirs(r))
        seen = set(out)
        out.extend(d for d in range(len(self.dir_ids)) if d not in seen)
        return out

    # ================================================================== masks
    def _cache_get(self, comp: tuple) -> Optional[_Mask]:
        m = self._masks.get(comp)
        if m is not None:
            self._masks.move_to_end(comp)
        return m

    def _cache_put(self, comp: tuple, m: _Mask) -> None:
        self._masks[comp] = m
        self._masks.move_to_end(comp)
        while len(self._masks) > MASK_CACHE:
            self._masks.popitem(last=False)

    _DEPS = {
        "kinds": frozenset({"upd", "ren"}), "exts": frozenset({"upd", "ren"}), "noext": frozenset({"upd", "ren"}),
        "size": frozenset({"upd"}), "mtime": frozenset({"upd"}), "ctime": frozenset({"upd"}),
        "folder": frozenset({"ren", "dirs"}), "path": frozenset({"ren", "dirs"}), "roots": frozenset({"ren", "dirs"}),
        "excl": frozenset({"upd", "ren", "dirs"}), "fav": frozenset({"ren", "dirs"}),
    }

    def _mask(self, comp: tuple) -> bytearray:
        n = self.n
        m = self._cache_get(comp)
        if m is None:
            m = _Mask(comp, self._build_comp(comp, 0, n), self._DEPS[comp[0]])
            self._cache_put(comp, m)
            self.stats["mask_build"] += 1
        elif len(m.data) < n:
            m.data.extend(self._build_comp(comp, len(m.data), n))
            self.stats["mask_extend"] += 1
        return m.data

    def _combined(self, comps: List[tuple]) -> bytearray:
        """AND of the component masks and ``live`` (cached; extended after appends)."""
        n = self.n
        if not comps:
            return self.live
        key = ("ALL", frozenset(comps))
        m = self._cache_get(key)
        if m is not None and len(m.data) == n:
            return m.data
        parts = [self._mask(c) for c in comps]   # extends/builds the components first
        if m is None:
            lo = 0
            data = bytearray()
        else:
            lo = len(m.data)
            data = m.data
        if lo < n:
            x = int.from_bytes(self.live[lo:n], "little")
            for p in parts:
                x &= int.from_bytes(p[lo:n], "little")
            data.extend(x.to_bytes(n - lo, "little"))
        if m is None:
            self._cache_put(key, _Mask(key, data, frozenset()))
            self.stats["mask_build_all"] += 1
        else:
            self.stats["mask_extend_all"] += 1
        return data

    def _dir_mask(self, spec: tuple) -> bytearray:
        """0/1 per dir slot (cached; extended when dirs are appended)."""
        nd = len(self.dir_ids)
        m = self._dir_masks.get(spec)
        if m is None:
            m = self._dir_masks[spec] = bytearray()
        if len(m) < nd:
            m.extend(self._build_dir_mask(spec, len(m), nd))
        return m

    def _build_dir_mask(self, spec: tuple, lo: int, hi: int) -> bytearray:
        kind = spec[0]
        if kind == "folder":
            key = spec[1]
            sep = _key_sep(key)
            pre = key + sep if not key.endswith(sep) else key
            pkeys = self._pkeys()
            return bytearray(1 if (k == key or k.startswith(pre)) else 0 for k in pkeys[lo:hi])
        if kind == "path":
            needle, case = spec[1], spec[2]
            src = self._dir_cs_keys() if case else self.dir_key
            return bytearray(map(contains, src[lo:hi], repeat(needle)))
        if kind == "dirend":
            part, case = spec[1], spec[2]
            src = self._dir_cs_keys() if case else self.dir_key
            return bytearray(map(methodcaller("endswith", part), src[lo:hi]))
        if kind == "roots":
            ids = spec[1]
            dr = self.dir_root
            return bytearray(map(ids.__contains__, dr[lo:hi]))
        if kind == "excl":
            return self._excl_dirs(spec[1], spec[2], spec[3], self._dir_masks.get(spec, bytearray())[:lo], hi)
        raise ValueError(spec)

    def _pkeys(self) -> List[str]:
        if self._dir_pkeys is None:
            self._dir_pkeys = []
        pk = self._dir_pkeys
        if len(pk) < len(self.dir_ids):
            pk.extend(path_key(p) for p in self.dir_path[len(pk):])
        return pk

    def _excl_dirs(self, names: frozenset, rxs: tuple, seqs: tuple, prefix: bytearray, nd: int) -> bytearray:
        """1 for dirs that are NOT excluded (no component below the root matches), for dir
        slots ``[len(prefix), nd)``; parents are decided before their children."""
        lo = len(prefix)
        out = bytearray(prefix) + bytearray(b"\x01" * (nd - lo))
        rx = re.compile("|".join(rxs)) if rxs else None
        order = self._dir_order() if lo == 0 else self._topo_slots(set(range(lo, nd)))
        for d in order:
            p = self.dir_parent[d]
            if p < 0:
                continue
            if not out[p]:
                out[d] = 0
                continue
            comps = self.dir_key[d].split("/")[1:]
            last = comps[-1] if comps else ""
            if last in names or (rx is not None and rx.match(last)):
                out[d] = 0
                continue
            for seq in seqs:
                k = len(seq)
                if len(comps) >= k and tuple(comps[-k:]) == seq:
                    out[d] = 0
                    break
        return out[lo:]

    def _build_comp(self, comp: tuple, lo: int, hi: int) -> bytearray:
        kind = comp[0]
        if lo >= hi:
            return bytearray()
        if kind == "kinds":
            x = 0
            for k in comp[1]:
                x |= int.from_bytes(self._kind_mask(k, lo, hi), "little")
            return bytearray(x.to_bytes(hi - lo, "little"))
        if kind in ("exts", "noext"):
            ids = frozenset(self._ext_id[e] for e in comp[1] if e in self._ext_id)
            m = bytearray(map(ids.__contains__, self.ext[lo:hi]))
            return m.translate(_T_NOT) if kind == "noext" else m
        if kind == "size":
            lo_v, hi_v = comp[1], comp[2]
            x = int.from_bytes(self.flags[lo:hi].translate(_T_FILE), "little")
            sz = self.size[lo:hi]
            if lo_v is not None:
                x &= int.from_bytes(bytearray(map(lo_v.__le__, sz)), "little")
            if hi_v is not None:
                x &= int.from_bytes(bytearray(map(hi_v.__gt__, sz)), "little")
            return bytearray(x.to_bytes(hi - lo, "little"))
        if kind in ("mtime", "ctime"):
            col = (self.mtime if kind == "mtime" else self.ctime)[lo:hi]
            m: Optional[bytearray] = None
            if comp[1] is not None:
                m = bytearray(map(comp[1].__le__, col))
            if comp[2] is not None:
                m2 = bytearray(map(comp[2].__gt__, col))
                m = m2 if m is None else bytearray(
                    (int.from_bytes(m, "little") & int.from_bytes(m2, "little")).to_bytes(hi - lo, "little"))
            return m if m is not None else bytearray(b"\x01" * (hi - lo))
        if kind in ("folder", "path", "roots"):
            spec = comp if kind != "path" else ("path", comp[1], comp[2])
            dm = self._dir_mask(spec)
            return bytearray(map(dm.__getitem__, self.dslot[lo:hi]))
        if kind == "excl":
            return self._excl_mask(comp, lo, hi)
        if kind == "fav":
            return self._fav_mask(comp, lo, hi)
        raise ValueError(comp)

    def _kind_mask(self, k: str, lo: int, hi: int) -> bytearray:
        fl = self.flags[lo:hi]
        if k == "folder":
            return fl.translate(_T_FOLDER)
        if k == "file":
            return fl.translate(_T_FILE)
        exts = _kinds.CATEGORIES.get(k, frozenset())
        ids = frozenset(self._ext_id[e] for e in exts if e in self._ext_id)
        m = bytearray(map(ids.__contains__, self.ext[lo:hi]))
        x = int.from_bytes(m, "little") & int.from_bytes(fl.translate(_T_FILE), "little")
        m = bytearray(x.to_bytes(hi - lo, "little"))
        keys = self.keys
        for s in compress(range(lo, hi), map(_NAME_KINDS.__contains__, keys[lo:hi])):
            m[s - lo] = 1 if (_NAME_KINDS[keys[s]] == k and not self.flags[s] & FOLDER) else 0
        return m

    def _excl_spec(self, patterns: Tuple[str, ...]) -> Tuple[frozenset, tuple, tuple, frozenset, frozenset]:
        """(names, other glob regexes, sequences, '*.ext' exts, '.ext' names) from patterns."""
        names: Set[str] = set()
        rxs: List[str] = []
        seqs: List[tuple] = []
        exts: Set[str] = set()
        dotnames: Set[str] = set()
        for raw in patterns:
            pat = raw.strip()
            if not pat:
                continue
            if "/" in pat or "\\" in pat:
                parts = tuple(fold(p) for p in pat.replace("\\", "/").split("/") if p)
                if parts:
                    seqs.append(parts)
            elif set("*?[") & set(pat):
                f = fold(pat)
                m = re.fullmatch(r"\*\.([^*?\[\]./\\]+)", f)
                if m:
                    exts.add(m.group(1))
                    dotnames.add("." + m.group(1))
                else:
                    import fnmatch
                    rxs.append(fnmatch.translate(f))
            else:
                names.add(fold(pat))
        return frozenset(names), tuple(rxs), tuple(seqs), frozenset(exts), frozenset(dotnames)

    def _excl_mask(self, comp: tuple, lo: int, hi: int) -> bytearray:
        names, rxs, seqs, exts, dotnames = comp[1:]
        # directories: '*.ext' globs apply to folder names too
        dir_rxs = rxs + tuple(r"(?s:.*\." + re.escape(e) + r")\Z" for e in exts)
        dm = self._dir_mask(("excl", names, dir_rxs, seqs))
        keys = self.keys[lo:hi]
        fix: List[int] = []
        x = int.from_bytes(bytearray(map(dm.__getitem__, self.dslot[lo:hi])), "little")
        bad = names | dotnames
        if bad:
            x &= ~int.from_bytes(bytearray(map(bad.__contains__, keys)), "little")
        if exts:
            ids = frozenset(self._ext_id[e] for e in exts if e in self._ext_id)
            if ids:
                x &= ~int.from_bytes(bytearray(map(ids.__contains__, self.ext[lo:hi])), "little")
            # folders carry no ext id: match their names directly
            fl = self.flags[lo:hi]
            for s in compress(range(lo, hi), fl.translate(_T_FOLDER)):
                k = self.keys[s]
                if any(k.endswith("." + e) for e in exts):
                    fix.append(s)
        if rxs:
            rx = re.compile("|".join(rxs))
            x &= ~int.from_bytes(bytearray(map(truth, map(rx.match, keys))), "little")
        if seqs:
            lasts = frozenset(sq[-1] for sq in seqs)
            for s in compress(range(lo, hi), map(lasts.__contains__, keys)):
                comps = self.dir_key[self.dslot[s]].split("/")[1:] + [self.keys[s]]
                for sq in seqs:
                    k = len(sq)
                    if len(comps) >= k and tuple(comps[-k:]) == sq:
                        fix.append(s)
                        break
        out = bytearray((x & ((1 << (8 * (hi - lo))) - 1)).to_bytes(hi - lo, "little"))
        for s in fix:
            out[s - lo] = 0
        return out

    def _fav_mask(self, comp: tuple, lo: int, hi: int) -> bytearray:
        favkeys: frozenset = comp[2]
        out = bytearray(hi - lo)
        if not favkeys:
            return out
        base = frozenset(fold(k.rstrip("\\/").replace("\\", "/").rsplit("/", 1)[-1]) for k in favkeys)
        for s in compress(range(lo, hi), map(base.__contains__, self.keys[lo:hi])):
            if path_key(_join(self.dir_path[self.dslot[s]], self.names[s])) in favkeys:
                out[s - lo] = 1
        return out

    # ================================================================== planning
    def _components(self, pq: ParsedQuery, filters: FilterState, favs: Optional[Tuple[int, frozenset]]) -> List[tuple]:
        comps: List[tuple] = []

        def add_fs(fs: FilterState, case: bool, needles: Sequence[str]) -> None:
            if fs.kinds:
                comps.append(("kinds", frozenset(fs.kinds)))
            if fs.exts:
                comps.append(("exts", frozenset(fold(e.lstrip(".")) for e in fs.exts)))
            if fs.size_min is not None or fs.size_max is not None:
                comps.append(("size", fs.size_min, fs.size_max))
            if fs.mtime_min is not None or fs.mtime_max is not None:
                comps.append(("mtime", ceil_time(fs.mtime_min), ceil_time(fs.mtime_max)))
            if fs.folder:
                comps.append(("folder", path_key(os.path.expanduser(fs.folder))))
            for nd in needles:
                comps.append(("path", nd, case))
            if fs.excludes:
                comps.append(("excl",) + self._excl_spec(tuple(fs.excludes)))
            if fs.favorites_only:
                comps.append(("fav",) + (favs if favs is not None else (0, frozenset())))

        chip_needles = []
        if filters.path_contains:
            nd = fold(filters.path_contains).replace("\\", "/")
            if nd:
                chip_needles.append(nd)
        add_fs(filters, False, chip_needles)
        add_fs(pq.token_filters, pq.case_sensitive, pq.path_needles)
        if pq.ctime_min is not None or pq.ctime_max is not None:
            comps.append(("ctime", ceil_time(pq.ctime_min), ceil_time(pq.ctime_max)))
        if pq.exclude_exts:
            comps.append(("noext", pq.exclude_exts))
        # dedupe, stable
        seen: Set[tuple] = set()
        return [c for c in comps if not (c in seen or seen.add(c))]

    def _atom_pred(self, a: Atom, case: bool):
        """Function: slot list -> iterable of truthy/falsy per slot."""
        src = self._cs() if case else self.keys
        get = src.__getitem__
        if a.kind == "sub":
            t = a.text
            return lambda c: map(contains, map(get, c), repeat(t))
        if a.kind == "glob":
            rx = re.compile(glob_regex(a.text))
            return lambda c: map(truth, map(rx.match, map(get, c)))
        # path atom
        dm = self._dir_mask(("dirend", a.dir_part, case)) if a.dir_part else None
        ds = self.dslot.__getitem__
        np_ = a.name_part
        if not np_:
            namef = None
        elif set("*?") & set(np_):
            rx = re.compile(glob_regex(np_))
            namef = lambda c: map(truth, map(rx.match, map(get, c)))  # noqa: E731
        else:
            namef = lambda c: map(methodcaller("startswith", np_), map(get, c))  # noqa: E731
        if dm is None and namef is None:
            return lambda c: repeat(True, len(c))
        if dm is None:
            return namef
        dget = dm.__getitem__
        if namef is None:
            return lambda c: map(dget, map(ds, c))
        return lambda c: map(min, map(dget, map(ds, c)), map(truth, namef(c)))

    def _text_plan(self, pq: ParsedQuery):
        """(primary scanner, verifiers). The primary returns candidate slots of a segment in
        slot order; each verifier filters a candidate list."""
        case = pq.case_sensitive
        verifiers: List[Callable[[list], list]] = []
        primary: Optional[Callable[[_Seg], list]] = None

        def keep(pred):
            return lambda c: list(compress(c, pred(c))) if c else c

        terms = list(pq.positive_terms)
        if terms:
            if case:
                ranked = sorted(terms, key=lambda t: -len(t))
                mode = "map"
            else:
                est = {t: self._estimate(t) for t in terms}
                ranked = sorted(terms, key=lambda t: (est[t], -len(t)))
                mode = "map" if est[ranked[0]] > self.n // FIND_BELOW else "find"
            first = ranked[0]
            primary = _TermScan(self, first, mode, case, est[first] if not case else self.n)
            for t in ranked[1:]:
                verifiers.append(keep(self._atom_pred(Atom("sub", t), case)))
        for a in pq.path_terms:
            pred = self._atom_pred(a, case)
            if primary is None and a.name_part:
                chunk = max(literal_chunks(a.name_part), key=len, default="")
                if chunk:
                    primary = self._chunk_primary(chunk, case)
            verifiers.append(keep(pred))
        for g in pq.globs:
            if primary is None:
                chunk = max(literal_chunks(g), key=len, default="")
                if chunk:
                    primary = self._chunk_primary(chunk, case)
            if g != "*":
                verifiers.append(keep(self._atom_pred(Atom("glob", g), case)))
        for group in pq.any_groups:
            preds = [self._atom_pred(a, case) for a in group]
            if primary is None and all(a.kind == "sub" for a in group):
                alts = [a.text for a in group]
                primary = self._union_primary(alts, case)
                continue

            def any_pred(c, preds=preds):
                acc = None
                for p in preds:
                    v = list(map(truth, p(c)))
                    acc = v if acc is None else list(map(or_, acc, v))
                return acc
            verifiers.append(lambda c, f=any_pred: list(compress(c, f(c))) if c else c)
        for a in pq.negative_terms:
            pred = self._atom_pred(a, case)
            verifiers.append(lambda c, p=pred: list(compress(c, map(not_, p(c)))) if c else c)
        if pq.regex:
            rx = re.compile(pq.regex, 0 if case else re.I)
            src = self._cs() if case else self.keys
            if primary is None and not case and _blob_safe(pq.regex):
                primary = self._regex_primary(pq.regex, rx)
            elif primary is None:
                primary = lambda seg: list(compress(range(seg.lo, seg.hi), map(rx.search, src[seg.lo:seg.hi])))  # noqa: E731
            else:
                get = src.__getitem__
                verifiers.append(lambda c: list(compress(c, map(rx.search, map(get, c)))) if c else c)
        return primary, verifiers

    def _regex_primary(self, pattern: str, rx):
        """Regex candidates from one search per match over the key blob (``re.M`` so ^/$ anchor
        at name edges), each verified against its own key; the next search starts at the next
        name, so every name is examined exactly once as a match start."""
        brx = re.compile(pattern, re.I | re.M)
        keys = self.keys

        def scan(seg):
            blob, offs = seg.blob, seg.offs
            search = brx.search
            br = bisect.bisect_right
            nl, end, base = len(offs), len(blob), seg.lo
            out: List[int] = []
            pos = 0
            while True:
                mo = search(blob, pos)
                if mo is None:
                    break
                r = br(offs, mo.start()) - 1
                s = base + r
                if rx.search(keys[s]):
                    out.append(s)
                pos = offs[r + 1] if r + 1 < nl else end
                if pos >= end:
                    break
            return out
        return scan

    def _chunk_primary(self, chunk: str, case: bool):
        mode = "map" if case or self._estimate(chunk) > self.n // FIND_BELOW else "find"
        return lambda seg: self._scan_sub(seg, chunk, mode, case)

    def _union_primary(self, alts: List[str], case: bool):
        modes = {t: ("map" if case or self._estimate(t) > self.n // FIND_BELOW else "find") for t in alts}

        def scan(seg):
            acc: Set[int] = set()
            for t in alts:
                acc.update(self._scan_sub(seg, t, modes[t], case))
            return sorted(acc)
        return scan

    # ================================================================== evaluation
    def _favs(self, user) -> Optional[Tuple[int, frozenset]]:
        if user is None:
            return None
        try:
            paths = user.favorite_paths()
            gen = int(getattr(user, "favorites_generation", 0))
        except Exception:  # user data unavailable: no favorites
            return (0, frozenset())
        return gen, frozenset(path_key(p) for p in paths)

    def _evaluate(self, pq: ParsedQuery, filters: FilterState, user, cancel: Optional[Callable[[], bool]],
                  stop_at: Optional[int] = None, root_counts: bool = False, t_cap: float = 0.0):
        """Return ``(hits, t4, root_counts)`` or None if cancelled. ``hits`` are the slots
        that match every constraint (root scope included), ascending; ``t4`` the extra
        abbreviation-tier slots."""
        self._ensure_blobs()
        favs = self._favs(user) if (filters.favorites_only or pq.token_filters.favorites_only) else None
        comps = self._components(pq, filters, favs)
        mask = self._combined(comps)
        rmask = None
        if filters.root_ids:
            rmask = self._mask(("roots", frozenset(filters.root_ids)))
        self._last_masks = (mask, rmask)
        primary, verifiers = self._text_plan(pq)
        t_start = time.perf_counter()
        pre: List[int] = []
        hits: List[int] = []
        mget = mask.__getitem__
        all_live = mask is self.live and not self.dead
        if isinstance(primary, _TermScan) and primary.mode == "map" and not all_live:
            # A selective filter (e.g. ext:js) with a common term: walk the mask, then check the
            # term on those keys only (cheaper than a full key scan plus a mask pass).
            if mask.count(1) < primary.est:
                src = self._cs() if primary.case else self.keys
                t = primary.term
                verifiers = [lambda c, t=t, g=src.__getitem__: list(compress(c, map(contains, map(g, c), repeat(t))))
                             ] + verifiers
                primary = None
        for seg in self._segs:
            if cancel is not None and cancel():
                return None
            if primary is None:
                if all_live:
                    c = list(range(seg.lo, seg.hi))
                else:
                    c = list(compress(range(seg.lo, seg.hi), mask[seg.lo:seg.hi]))
            else:
                c = primary(seg)
                if c and not all_live:
                    c = list(compress(c, map(mget, c)))
            for v in verifiers:
                if not c:
                    break
                c = v(c)
            if rmask is not None:
                pre.extend(c)
                c = list(compress(c, map(rmask.__getitem__, c))) if c else c
            hits.extend(c)
            if stop_at is not None and len(hits) >= stop_at:
                break
            if t_cap and time.perf_counter() - t_start > t_cap:
                raise TimeoutError("The regular expression took too long; make it more specific.")
        if rmask is None:
            pre = hits
        if cancel is not None and cancel():
            return None
        # abbreviation tier (A.7): single plain term, few substring hits
        t4: List[int] = []
        t4_pre: List[int] = []
        q = self._abbrev_term(pq)
        if q and len(hits) < ABBREV_BELOW and (stop_at is None or len(hits) < stop_at):
            have = set(pre)
            for seg in self._segs:
                self._ensure_iblob(seg)
                found = self._scan_iblob(seg, q)
                t4_pre.extend(s for s in found if mask[s] and s not in have)
            t4 = [s for s in t4_pre if rmask[s]] if rmask is not None else t4_pre
        rc: tuple = ()
        if root_counts:
            cnt = self._root_count(pre)
            if t4_pre:
                cnt.update(self._root_count(t4_pre))
            rc = tuple((r, cnt.get(r, 0)) for r in sorted(self.root_slot))
        return hits, t4, rc

    def _root_count(self, slots: List[int]) -> Counter:
        """Hits per root id for an ascending slot list: O(runs * log) via the root runs."""
        if len(self.root_slot) <= 1 and self._runs is not None:
            return Counter({next(iter(self.root_slot)): len(slots)}) if self.root_slot else Counter()
        if self._runs is None:
            roots = list(map(self.dir_root.__getitem__, self.dslot))
            runs: List[Tuple[int, int]] = [(0, roots[0])] if roots else []
            runs.extend((j, roots[j]) for j in compress(range(1, len(roots)), map(ne, roots[1:], roots)))
            self._runs = runs
        runs = self._runs
        if len(runs) > 4096:
            dr, ds = self.dir_root, self.dslot
            return Counter(map(dr.__getitem__, map(ds.__getitem__, slots)))
        cnt: Counter = Counter()
        bl = bisect.bisect_left
        prev = 0
        for i, (start, r) in enumerate(runs):
            end = runs[i + 1][0] if i + 1 < len(runs) else self.n
            hi = bl(slots, end, prev)
            if hi > prev:
                cnt[r] += hi - prev
            prev = hi
        return cnt

    @staticmethod
    def _abbrev_term(pq: ParsedQuery) -> Optional[str]:
        if (len(pq.positive_terms) == 1 and not pq.case_sensitive and not pq.any_groups and not pq.globs
                and not pq.path_terms and not pq.regex and not pq.negative_terms):
            q = pq.positive_terms[0]
            if 2 <= len(q) <= 8 and q.isalnum():
                return q
        return None

    def _scan_iblob(self, seg: _Seg, term: str) -> List[int]:
        blob, offs = seg.iblob, seg.ioffs
        find = blob.find
        br = bisect.bisect_right
        nl, end, base = len(offs), len(blob), seg.lo
        out: List[int] = []
        pos = find(term)
        while pos >= 0:
            r = br(offs, pos) - 1
            out.append(base + r)
            pos = find(term, offs[r + 1] if r + 1 < nl else end)
        return out

    # ================================================================== ranking
    def _relevance(self, st: _State, cancel) -> Optional[list]:
        """Tiers [exact, prefix, boundary, substring, abbrev] as ``_Tier`` objects (A.7)."""
        if st.rel is not None:
            return st.rel
        hits = st.hits
        q = st.pq.first_term()
        if q is not None and st.pq.case_sensitive:
            q = fold(q)
        if not q:
            st.rel = [self._ordered_tier(hits, st), _Tier(items=st.t4)]
            return st.rel
        keys = self.keys
        pm = list(map(methodcaller("startswith", q), map(keys.__getitem__, hits)))
        pref = list(compress(hits, pm))
        pk = list(map(keys.__getitem__, pref))
        # exact: the name, or the name without its last extension, equals the term
        ex = list(map(or_, map(q.__eq__, pk),
                      map(q.__eq__, map(itemgetter(0), map(methodcaller("rpartition", "."), pk)))))
        t0 = list(compress(pref, ex))
        t1 = list(compress(pref, map(not_, ex)))
        if cancel is not None and cancel():
            return None
        sr = self.srank
        n_rest = len(hits) - len(pref)
        if n_rest <= TIER2_EXACT:
            rest = list(compress(hits, map(not_, pm)))
            window = rest
        else:
            # The TIER2_WINDOW best-static-rank non-prefix hits, in srank order (one fused pass).
            vals = heapq.nsmallest(TIER2_WINDOW, compress(map(sr.__getitem__, hits), map(not_, pm)))
            window = list(map(and_, vals, repeat(SLOT_MASK)))
            rest = None
        bm = self.bmask
        t2: List[int] = []
        for s in window:
            b = bm[s] or self._bounds(s)
            k = keys[s]
            p = k.find(q)
            while 0 <= p < 64:
                if (b >> p) & 1:
                    t2.append(s)
                    break
                p = k.find(q, p + 1)
        t2set = set(t2)
        if rest is not None:
            t3 = _Tier(items=list(compress(rest, map(not_, map(t2set.__contains__, rest)))) if t2 else rest)
            tier2 = _Tier(items=t2)
        else:
            # Everything outside the window ranks after it, so the window minus t2 is a correct
            # srank-ordered prefix of tier 3; the full tier is only built for deep paging.
            def make(hits=hits, pm=pm, t2set=t2set):
                r = list(compress(hits, map(not_, pm)))
                return list(compress(r, map(not_, map(t2set.__contains__, r))))
            t3 = _Tier(order=[x for x in window if x not in t2set], size=n_rest - len(t2), make=make)
            tier2 = _Tier(order=t2)
        st.rel = [_Tier(items=t0), _Tier(items=t1), tier2, t3, _Tier(items=st.t4)]
        return st.rel

    def _ordered_tier(self, hits: List[int], st: _State) -> _Tier:
        """Tier for a no-text (filters only) result: when the index is not changing, its order is
        the cached global srank order filtered by the result mask, so the first page costs
        O(page / density) instead of a heap over every hit."""
        if len(hits) < WALK_ABOVE or self.partial:
            return _Tier(items=hits)
        order = self._global_order()
        member = st.member
        if member is None:
            return _Tier(items=hits)
        tier = _Tier(items=hits)
        tier.make = lambda m, order=order, member=member: list(islice(compress(order, map(member.__getitem__, order)), m))
        tier.order = None
        tier.size = len(hits)
        st.walk = tier
        return tier

    def _global_order(self) -> List[int]:
        key = (self.generation, self.epoch, self.n)
        if self._gorder_key != key:
            self._gorder = list(map(and_, sorted(compress(self.srank, self.live)), repeat(SLOT_MASK)))
            self._gorder_key = key
        return self._gorder

    def _tier_prefix(self, t: _Tier, m: int) -> List[int]:
        if t.make is not None and t.items is not None and t.order is None and m <= HEAP_PAGES:
            return t.make(m)        # global-order walk (filters-only results)
        if m <= 0 or t.size == 0:
            return []
        if t.order is not None and (len(t.order) >= m or len(t.order) >= t.size):
            return t.order[:m]
        if t.items is None:
            t.items = t.make()
        sr = self.srank
        if m >= t.size or m > HEAP_PAGES:
            t.order = list(map(and_, sorted(map(sr.__getitem__, t.items)), repeat(SLOT_MASK)))
            return t.order[:m]
        return list(map(and_, heapq.nsmallest(m, map(sr.__getitem__, t.items)), repeat(SLOT_MASK)))

    def _rel_prefix(self, st: _State, m: int, cancel=None) -> Optional[List[int]]:
        tiers = self._relevance(st, cancel)
        if tiers is None:
            return None
        out: List[int] = []
        for t in tiers:
            need = m - len(out)
            if need <= 0:
                break
            out.extend(self._tier_prefix(t, need))
        return out

    def _sort_order(self, key: str, desc: bool) -> List[int]:
        """Every slot ordered by (value, name, slot) for a sort key, value descending if
        ``desc``; cached per index generation (one full sort, then reused by every query)."""
        stamp = (self.generation, self.epoch, self.n)
        ck = (key, desc)
        hit = self._sort_orders.get(ck)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        name = self._sort_orders.get(("name", False))
        if name is not None and name[0] == stamp:
            by_name = name[1]
        else:
            by_name = sorted(range(self.n), key=self.keys.__getitem__)
            self._sort_orders[("name", False)] = (stamp, by_name)
        if key == "name":
            order = by_name if not desc else sorted(by_name, key=self.keys.__getitem__, reverse=True)
        else:
            vals = self._slot_vals(key)
            order = sorted(by_name, key=vals.__getitem__, reverse=desc)
        if len(self._sort_orders) > 4:   # bound memory: keep the name order + a few others
            for k in [k for k in self._sort_orders if k != ("name", False) and k != ck][:-2]:
                del self._sort_orders[k]
        self._sort_orders[ck] = (stamp, order)
        self.stats["sort_order_build"] += 1
        return order

    def _slot_vals(self, key: str) -> list:
        """Per-slot sort value for ``key`` (same values as ``_sort_vals``, for every slot)."""
        if key == "size":
            vals = self.size.tolist()
            fl = self.flags
            for s in compress(range(self.n), fl.translate(_T_FOLDER)):
                vals[s] = -1
            return vals
        if key == "mtime":
            return self.mtime.tolist()
        if key == "folder":
            dr = self._dir_ranks()
            return list(map(dr.__getitem__, self.dslot))
        if key == "kind":
            er = self._ext_ranks()
            return list(map(er.__getitem__, self.ext))
        raise ValueError(f"unknown sort key {key!r}")

    def _sort_vals(self, st: _State) -> list:
        if st.vals is not None:
            return st.vals
        key = st.sort.key
        hits = st.hits
        if key == "size":
            vals = list(map(self.size.__getitem__, hits))
            fl = self.flags
            for i in compress(range(len(hits)), map(_T_FOLDER.__getitem__, map(fl.__getitem__, hits))):
                vals[i] = -1
        elif key == "mtime":
            vals = list(map(self.mtime.__getitem__, hits))
        elif key == "folder":
            dr = self._dir_ranks()
            vals = list(map(dr.__getitem__, map(self.dslot.__getitem__, hits)))
        elif key == "kind":
            er = self._ext_ranks()
            vals = list(map(er.__getitem__, map(self.ext.__getitem__, hits)))
        else:
            raise ValueError(f"unknown sort key {key!r}")
        st.vals = vals
        return vals

    def _dir_ranks(self) -> array:
        if self._dir_rank is None or len(self._dir_rank) != len(self.dir_ids):
            keys = [fold(p) for p in self.dir_path]
            order = sorted(range(len(keys)), key=keys.__getitem__)
            rank = array("q", bytes(8 * len(keys)))
            for r, d in enumerate(order):
                rank[d] = r
            self._dir_rank = rank
        return self._dir_rank

    def _ext_ranks(self) -> array:
        if self._ext_rank is None or len(self._ext_rank) != len(self.ext_names):
            order = sorted(range(len(self.ext_names)), key=self.ext_names.__getitem__)
            rank = array("q", bytes(8 * len(order)))
            for r, e in enumerate(order):
                rank[e] = r
            self._ext_rank = rank
        return self._ext_rank

    def _sort_prefix(self, st: _State, m: int) -> List[int]:
        """First ``m`` hits for a non-relevance sort: (value, name, slot); descending flips
        the value only (ties still by name, then slot)."""
        hits = st.hits
        desc = st.sort.descending
        kget = self.keys.__getitem__
        if len(hits) > FULL_SORT_BELOW and m <= HEAP_PAGES and not self.partial:
            # Large result on a settled index: walk the cached global order for this sort and
            # keep the hits (C-level, lazy; stops after m), instead of passes over every hit.
            if st.hitset is None:
                st.hitset = set(hits)
            order = self._sort_order(st.sort.key, desc)
            return list(islice(compress(order, map(st.hitset.__contains__, order)), m))
        if st.sort.key == "name":
            if st.full is None and (len(hits) <= FULL_SORT_BELOW or m > HEAP_PAGES):
                st.full = sorted(hits, key=kget, reverse=desc)
            if st.full is not None:
                return st.full[:m]
            return (heapq.nlargest if desc else heapq.nsmallest)(m, hits, key=kget)
        vals = self._sort_vals(st)
        if st.full is None and (len(hits) <= FULL_SORT_BELOW or m > HEAP_PAGES):
            names = list(map(kget, hits))
            order = sorted(range(len(hits)), key=names.__getitem__)
            order.sort(key=vals.__getitem__, reverse=desc)
            st.full = [hits[i] for i in order]
        if st.full is not None:
            return st.full[:m]
        n = len(hits)
        idx = range(n)
        if m <= 0 or not n:
            return []
        # top m by value with an integer heap: (value + offset) * 2^32 + index (no key function)
        big = 1 << 32
        comp = map(add, map(mul, map(add, vals, repeat(_VAL_OFFSET)), repeat(big)), idx)
        if desc:
            top = [c & (big - 1) for c in heapq.nlargest(m, comp)]
        else:
            top = [c & (big - 1) for c in heapq.nsmallest(m, comp)]
        # the last value may be shared with hits outside the heap: decide that group by name
        v = vals[top[-1]]
        inside = [i for i in top if vals[i] != v]
        ties = list(compress(idx, map(v.__eq__, vals)))
        tk = list(map(kget, map(hits.__getitem__, ties)))
        ties = [ties[j] for j in sorted(range(len(ties)), key=tk.__getitem__)]
        chosen = sorted(inside + ties[:m - len(inside)])
        ck = list(map(kget, map(hits.__getitem__, chosen)))
        order = sorted(range(len(chosen)), key=ck.__getitem__)          # (name, slot)
        cv = list(map(vals.__getitem__, chosen))
        order.sort(key=cv.__getitem__, reverse=desc)                     # then value
        return [hits[chosen[j]] for j in order]

    def _prefix(self, st: _State, m: int, cancel=None) -> Optional[List[int]]:
        if st.sort.key == "relevance":
            return self._rel_prefix(st, m, cancel)
        out = self._sort_prefix(st, m)
        if len(out) < m and st.t4:   # abbreviation hits trail any explicit sort
            out = out + st.t4[:m - len(out)]
        return out

    # ================================================================== rows
    def _rows(self, st: _State, slots: Sequence[int], favkeys: Optional[frozenset]) -> tuple:
        case = st.case
        terms = st.spans_terms
        t4 = set(st.t4) if st.t4 else ()
        abbrev = st.abbrev
        rows = []
        for s in slots:
            d = self.dslot[s]
            dp = self.dir_path[d]
            name = self.names[s]
            path = _join(dp, name)
            rid = self.dir_root[d]
            rs = self.root_slot.get(rid)
            rp = self.dir_path[rs] if rs is not None else ""
            rel = dp[len(rp):].lstrip(_SEPS) if rp and dp.startswith(rp) else ("" if d == rs else dp)
            if s in t4 and abbrev:
                spans = initials_spans(name, abbrev)
            elif st.rx is not None:
                spans = regex_spans(name, st.rx, case)
            else:
                spans = match_spans(name, terms, case)
            fav = bool(favkeys) and path_key(path) in favkeys  # type: ignore[operator]
            rows.append(ResultRow(self.ids[s], name, path, rid, rel, self.size[s], self.mtime[s], self.flags[s],
                                  tuple(spans), fav))
        return tuple(rows)

    # ================================================================== search API
    def search(self, req: SearchRequest, cancel: Callable[[], bool], user) -> Optional[SearchResult]:
        """Run ``req``; None if ``cancel()`` turned true. Never raises for bad input."""
        t0 = time.perf_counter()
        pq = parse(req.text, now=self.clock())
        if pq.error:
            return SearchResult(gen=req.gen, total=0, offset=0, rows=(), elapsed_ms=_ms(t0), error=pq.error,
                                index_generation=self.generation, partial=self.partial)
        with self._lock:
            empty = pq.is_empty() and req.filters.is_empty()
            if empty:
                sugg = self._history_suggestions(req, user, t0) if req.suggestions else ()
                self._state = None
                return SearchResult(gen=req.gen, total=0, offset=0, rows=(), elapsed_ms=_ms(t0),
                                    suggestions=sugg, index_generation=self.generation, partial=self.partial)
            try:
                res = self._evaluate(pq, req.filters, user, cancel, root_counts=req.root_counts,
                                     t_cap=REGEX_TIME_CAP if pq.regex else 0.0)
            except TimeoutError as exc:
                return SearchResult(gen=req.gen, total=0, offset=0, rows=(), elapsed_ms=_ms(t0), error=str(exc),
                                    index_generation=self.generation, partial=self.partial)
            if res is None:
                return None
            hits, t4, rc = res
            st = _State()
            st.gen, st.epoch, st.pq, st.sort = req.gen, self.epoch, pq, req.sort
            st.hits, st.t4, st.total = hits, t4, len(hits) + len(t4)
            st.case = pq.case_sensitive
            st.spans_terms = pq.highlight_terms()
            st.rx = re.compile(pq.regex, 0 if pq.case_sensitive else re.I) if pq.regex else None
            st.abbrev = self._abbrev_term(pq) if t4 else None
            st.generation = self.generation
            st.root_counts = rc
            if not pq.has_text() and not t4:
                mask, rmask = self._last_masks
                if rmask is not None:
                    n = self.n
                    mask = bytearray((int.from_bytes(mask, "little") & int.from_bytes(rmask[:n], "little"))
                                     .to_bytes(n, "little"))
                st.member = mask
            limit = max(0, int(req.limit))
            top = self._prefix(st, limit, cancel)
            if top is None or (cancel is not None and cancel()):
                return None
            favs = self._favs(user)
            rows = self._rows(st, top, favs[1] if favs else None)
            sugg: tuple = ()
            if req.suggestions and not pq.regex:
                positive = bool(pq.positive_terms or pq.globs or pq.path_terms or pq.any_groups)
                names = self._name_suggestions(st, req, cancel) if positive else ()
                sugg = self._order_suggestions(names + self._history_suggestions(req, user, t0))
                if cancel is not None and cancel():
                    return None
            dym = None
            if st.total == 0 and pq.positive_terms and not any(seg.blob.count(pq.positive_terms[0])
                                                               for seg in self._segs):
                dym = self._did_you_mean(pq)
            self._state = st
            return SearchResult(gen=req.gen, total=st.total, offset=0, rows=rows, elapsed_ms=_ms(t0),
                                root_counts=rc, suggestions=sugg, did_you_mean=dym,
                                index_generation=self.generation, partial=self.partial,
                                terms=tuple(st.spans_terms))

    def page(self, gen: int, offset: int, limit: int) -> Optional[SearchResult]:
        """Rows ``[offset, offset+limit)`` of the last search with client ``gen`` (None if
        that search is no longer the latest or the index was compacted since)."""
        t0 = time.perf_counter()
        with self._lock:
            st = self._state
            if st is None or st.gen != gen or st.epoch != self.epoch:
                return None
            offset = max(0, int(offset))
            limit = max(0, int(limit))
            top = self._prefix(st, offset + limit)
            slots = (top or [])[offset:offset + limit]
            rows = self._rows(st, slots, None)
            return SearchResult(gen=gen, total=st.total, offset=offset, rows=rows, elapsed_ms=_ms(t0),
                                root_counts=st.root_counts, index_generation=self.generation,
                                partial=self.partial, terms=tuple(st.spans_terms))

    def count(self, text: str, filters: FilterState, stop_at: Optional[int] = None) -> int:
        """Exact number of hits for ``text`` under ``filters`` (or ``stop_at`` once reached).
        An empty text with empty filters is "no query" and returns 0, like ``search``; use
        ``counters().total_entries`` for the size of the index."""
        pq = parse(text, now=self.clock())
        if pq.error:
            return 0
        with self._lock:
            return self._count_locked(pq, filters, stop_at, None)

    def _count_locked(self, pq: ParsedQuery, filters: FilterState, stop_at: Optional[int], user) -> int:
        if pq.is_empty() and filters.is_empty():
            return 0
        res = self._evaluate(pq, filters, user, None, stop_at=stop_at)
        if res is None:
            return 0
        hits, t4, _ = res
        total = len(hits) + len(t4)
        return min(total, stop_at) if stop_at is not None else total

    # ================================================================== suggestions (E.1)
    def _name_suggestions(self, st: _State, req: SearchRequest, cancel) -> tuple:
        if not st.total:
            return ()
        top = self._rel_prefix(st, SUGGEST_FROM, cancel)
        if not top:
            return ()
        keys = self.keys
        cnt = Counter(map(keys.__getitem__, top))
        typed = fold(req.text.strip())
        out = []
        seen: Set[str] = set()
        for s in top:
            k = keys[s]
            if k in seen or k == typed:
                continue
            seen.add(k)
            out.append(Suggestion("name", self.names[s], cnt[k]))
            if len(out) >= 5:
                break
        return tuple(out)

    def _history_suggestions(self, req: SearchRequest, user, t0: float) -> tuple:
        if user is None:
            return ()
        text = req.text.strip()
        try:
            items = user.history(prefix=text, limit=20)
        except Exception:
            return ()
        want = 3 if text else 5
        typed = fold(text)
        out = []
        deadline = time.perf_counter() + HISTORY_BUDGET
        for it in items:
            if len(out) >= want or time.perf_counter() > deadline:
                break
            qtext = it.query if hasattr(it, "query") else it[0]
            if fold(qtext.strip()) == typed:
                continue
            key = (qtext, req.filters, self.generation, self.epoch)
            ok = self._hist_cache.get(key)
            if ok is None:
                pq = parse(qtext, now=self.clock())
                ok = not pq.error and not (pq.is_empty() and req.filters.is_empty()) and \
                    self._count_locked(pq, req.filters, 1, user) > 0
                if len(self._hist_cache) > 512:
                    self._hist_cache.clear()
                self._hist_cache[key] = ok
            if ok:
                # Verified to have >= 1 hit under the current filters (count stops at 1, E.1);
                # without filters the count recorded with the history entry is shown.
                count = it.count if (req.filters.is_empty() and it.count > 0) else 1
                out.append(Suggestion("history", qtext, count))
        return tuple(out)

    @staticmethod
    def _order_suggestions(sugg: tuple) -> tuple:
        hist = [s for s in sugg if s.kind == "history"]
        names = [s for s in sugg if s.kind == "name"]
        return tuple(hist + names)[:8]

    def _did_you_mean(self, pq: ParsedQuery) -> Optional[str]:
        """A.7: one-edit variants of a single plain 4-24 char term, scored by blob counts."""
        if (len(pq.positive_terms) != 1 or pq.any_groups or pq.globs or pq.path_terms or pq.regex
                or pq.negative_terms or pq.case_sensitive):
            return None
        t = pq.positive_terms[0]
        if not 4 <= len(t) <= 24 or "\n" in t:
            return None
        deadline = time.perf_counter() + DID_YOU_MEAN_BUDGET
        blobs = [s.blob for s in self._segs]
        variants: List[Tuple[str, Optional[str]]] = []     # (literal or regex source, kind)
        seen: Set[str] = set()
        for i in range(len(t)):
            v = t[:i] + t[i + 1:]
            if v not in seen:
                seen.add(v)
                variants.append((v, None))
        for i in range(len(t) - 1):
            if t[i] != t[i + 1]:
                v = t[:i] + t[i + 1] + t[i] + t[i + 2:]
                if v not in seen:
                    seen.add(v)
                    variants.append((v, None))
        # one missing / one wrong character (with a literal prefix, so the regex engine can skip)
        for i in range(1, len(t) + 1):
            variants.append((re.escape(t[:i]) + "[^\n]" + re.escape(t[i:]), "ins"))
        for i in range(1, len(t)):
            variants.append((re.escape(t[:i]) + "[^\n]" + re.escape(t[i + 1:]), "sub"))
        best: Optional[Tuple[int, int, str]] = None
        for src, kind in variants[:40 + len(t) * 2]:
            if time.perf_counter() > deadline:
                break
            if kind is None:
                c = sum(b.count(src) for b in blobs)
                text = src
            else:
                found = Counter()
                rx = re.compile(src)
                for b in blobs:
                    found.update(rx.findall(b))
                found.pop(t, None)
                if not found:
                    continue
                text, _ = min(found.items(), key=lambda kv: (-kv[1], kv[0]))
                c = sum(found.values())
            if c <= 0 or text == t:
                continue
            cand = (c, len(text), text)
            if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                best = cand
        return best[2] if best else None


_BLOB_UNSAFE = re.compile(r"\\[AZsSWDnxuUN0-9]|\[\^|\(\?|\\b|\\B")


def _blob_safe(pattern: str) -> bool:
    """True if a regex cannot match across (or depend on) the newline between two keys in the
    blob, so searching the blob finds exactly the names a per-key search finds."""
    return not _BLOB_UNSAFE.search(pattern)


def _runs(ops: Sequence[tuple]):
    """Group an op list into runs for ``apply``: maximal runs of INS/DIR ops (yielded with code
    INS: their DIRs are applied first, then one vectorized append) and runs of other equal codes.
    The writer interleaves INS and DIR per directory, so this keeps batches large."""
    i, n = 0, len(ops)
    while i < n:
        code = ops[i][0]
        j = i + 1
        if code in (INS, DIR):
            while j < n and ops[j][0] in (INS, DIR):
                j += 1
            yield INS, ops[i:j]
        else:
            while j < n and ops[j][0] == code:
                j += 1
            yield code, ops[i:j]
        i = j


class _TermScan:
    """Primary scan for a plain term; keeps the term and its estimated hits so that a filter
    mask that is more selective than the term can be iterated first instead."""
    __slots__ = ("index", "term", "mode", "case", "est")

    def __init__(self, index, term, mode, case, est):
        self.index, self.term, self.mode, self.case, self.est = index, term, mode, case, est

    def __call__(self, seg):
        return self.index._scan_sub(seg, self.term, self.mode, self.case)


class _Tier:
    """One relevance tier: ``items`` (any order) and/or ``order`` (srank order: complete, or a
    correct prefix), ``size`` slots in total; ``make`` materializes ``items`` on demand."""
    __slots__ = ("items", "order", "size", "make")

    def __init__(self, items=None, order=None, size=None, make=None):
        self.items = items
        self.order = order
        self.size = size if size is not None else len(items if items is not None else order)
        self.make = make


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0
