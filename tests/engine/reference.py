"""Brute-force reference evaluator for the search engine (design H.1).

A plain Python loop over every row, written for obviousness rather than speed. It shares only
the *definitions* with the engine (``fold``/``nfc``, the parser, the kind table, the static
rank formula), never its evaluation code (masks, blobs, plans, tiers). Tests assert that
``MemIndex.search`` returns exactly the same total, id set, root counts and order.

Also here: ``gen_ops`` (a deterministic random tree as an op stream, with tricky names) and
``random_ops`` (a random mutation stream) used by the tests and the local benchmark.
"""
from __future__ import annotations

import fnmatch
import os
import random
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from fast_file_search import kinds
from fast_file_search.engine.memindex import path_key, static_rank
from fast_file_search.engine.ops import DEL, DIR, DIRPATH, INS, REN, UPD
from fast_file_search.engine.protocol import FOLDER, HIDDEN, PACKAGE, FilterState, SortSpec
from fast_file_search.engine.query import ParsedQuery, glob_regex, parse
from fast_file_search.engine.textfold import fold, nfc

SEP = "\\" if os.name == "nt" else "/"
_SPLIT = re.compile(r"[\\/]") if os.name == "nt" else re.compile("/")


@dataclass
class Row:
    id: int
    name: str
    key: str
    cs: str
    ext: str
    dir_path: str
    root_id: int
    comps: Tuple[str, ...]      # folded folder components below the root
    dir_key: str                # folded "rootname/comp/comp"
    dir_cs: str                 # NFC counterpart
    size: int
    mtime: int
    ctime: int
    flags: int
    srank: int
    path: str


def _join(d: str, name: str) -> str:
    if d.endswith(("/", "\\")):
        return d + name
    if "\\" in d and (os.name == "nt" or "/" not in d):
        return d + "\\" + name
    if "/" in d:
        return d + "/" + name
    return d + os.sep + name


class Reference:
    def __init__(self, entries: Sequence[tuple], dirs: Sequence[tuple], now: float):
        """``entries``: (id, dir_id, name, size, mtime, ctime, flags); ``dirs``: (id, parent_id,
        root_id, path, mtime, flags) as stored (``Store.iter_load``)."""
        self.now = now
        dmap = {d[0]: d for d in dirs}
        self.root_ids = sorted(d[0] for d in dirs if d[1] == 0)
        info: Dict[int, tuple] = {}

        def dir_info(did: int) -> tuple:
            if did in info:
                return info[did]
            _, parent, root, path, _, _ = dmap[did]
            if parent == 0:
                base = path.rstrip("\\/") or path
                rname = _SPLIT.split(base)[-1] or base
                res = ((), fold(rname), nfc(rname), False, root)
            else:
                pc, pk, pcs, phid, proot = dir_info(parent)
                ppath = dmap[parent][3]
                bn = path[len(ppath):].lstrip("\\/") if path.startswith(ppath) else _SPLIT.split(path.rstrip("\\/"))[-1]
                fb = fold(bn).replace("\\", "/")
                res = (pc + (fb,), pk + "/" + fb, pcs + "/" + nfc(bn).replace("\\", "/"),
                       phid or bn.startswith("."), proot)
            info[did] = res
            return res

        self.rows: List[Row] = []
        for (i, did, name, size, mtime, ctime, flags) in entries:
            comps, dkey, dcs, dhid, root = dir_info(did)
            key = fold(name)
            ext = "" if (flags & FOLDER and not flags & PACKAGE) else kinds.ext_of_key(key)
            hidden = bool(flags & HIDDEN) or key.startswith(".") or dhid
            sr = static_rank(hidden, len(comps), mtime, len(key), bool(flags & FOLDER), now)
            dpath = dmap[did][3]
            self.rows.append(Row(i, name, key, nfc(name), ext, dpath, root, comps, dkey, dcs, size, mtime, ctime,
                                 flags & 0xFF, sr, _join(dpath, name)))
        self.rows.sort(key=lambda r: r.id)

    @classmethod
    def from_store(cls, store, now: float) -> "Reference":
        entries: List[tuple] = []
        dirs: List[tuple] = []
        for chunk in store.iter_load():
            entries.extend(chunk.entries)
            dirs.extend(chunk.dirs)
        return cls(entries, dirs, now)

    # ------------------------------------------------------------------ predicates
    @staticmethod
    def _atom(a, r: Row, case: bool) -> bool:
        src = r.cs if case else r.key
        if a.kind == "sub":
            return a.text in src
        if a.kind == "glob":
            return re.match(glob_regex(a.text), src) is not None
        dk = r.dir_cs if case else r.dir_key
        if a.dir_part and not dk.endswith(a.dir_part):
            return False
        np_ = a.name_part
        if not np_:
            return True
        if "*" in np_ or "?" in np_:
            return re.match(glob_regex(np_), src) is not None
        return src.startswith(np_)

    def _text(self, pq: ParsedQuery, r: Row) -> bool:
        cs = pq.case_sensitive
        src = r.cs if cs else r.key
        from fast_file_search.engine.query import Atom
        if any(t not in src for t in pq.positive_terms):
            return False
        if any(not self._atom(Atom("glob", g), r, cs) for g in pq.globs):
            return False
        if any(not self._atom(a, r, cs) for a in pq.path_terms):
            return False
        if any(not any(self._atom(a, r, cs) for a in grp) for grp in pq.any_groups):
            return False
        if any(self._atom(a, r, cs) for a in pq.negative_terms):
            return False
        if pq.regex and re.search(pq.regex, src, 0 if cs else re.I) is None:
            return False
        return True

    @staticmethod
    def _excluded(r: Row, patterns: Sequence[str]) -> bool:
        parts = list(r.comps) + [r.key]
        for raw in patterns:
            pat = raw.strip()
            if not pat:
                continue
            if "/" in pat or "\\" in pat:
                seq = [fold(p) for p in pat.replace("\\", "/").split("/") if p]
                n = len(seq)
                if n and any(parts[i:i + n] == seq for i in range(len(parts) - n + 1)):
                    return True
            elif set("*?[") & set(pat):
                f = fold(pat)
                if any(fnmatch.fnmatchcase(p, f) for p in parts):
                    return True
            elif fold(pat) in parts:
                return True
        return False

    def _filters(self, fs: FilterState, r: Row, case: bool, needles: Sequence[str], favkeys: frozenset) -> bool:
        is_folder = bool(r.flags & FOLDER)
        if fs.kinds:
            ok = False
            for k in fs.kinds:
                if k == "folder":
                    ok |= is_folder
                elif k == "file":
                    ok |= not is_folder
                else:
                    ok |= (not is_folder) and kinds.category_of_key(r.key, r.flags) == k
            if not ok:
                return False
        if fs.exts and r.ext not in {fold(e.lstrip(".")) for e in fs.exts}:
            return False
        if fs.size_min is not None and (is_folder or r.size < fs.size_min):
            return False
        if fs.size_max is not None and (is_folder or r.size >= fs.size_max):
            return False
        if fs.mtime_min is not None and r.mtime < fs.mtime_min:
            return False
        if fs.mtime_max is not None and r.mtime >= fs.mtime_max:
            return False
        if fs.folder:
            fk = path_key(os.path.expanduser(fs.folder))
            dk = path_key(r.dir_path)
            sep = "\\" if os.name == "nt" else "/"
            if not (dk == fk or dk.startswith(fk if fk.endswith(sep) else fk + sep)):
                return False
        for nd in needles:
            if nd not in (r.dir_cs if case else r.dir_key):
                return False
        if fs.excludes and self._excluded(r, fs.excludes):
            return False
        if fs.favorites_only and path_key(r.path) not in favkeys:
            return False
        return True

    def _passes(self, pq: ParsedQuery, filters: FilterState, r: Row, favkeys: frozenset) -> bool:
        chip_needles = [fold(filters.path_contains).replace("\\", "/")] if filters.path_contains else []
        chip_needles = [n for n in chip_needles if n]
        if not self._filters(filters, r, False, chip_needles, favkeys):
            return False
        if not self._filters(pq.token_filters, r, pq.case_sensitive, pq.path_needles, favkeys):
            return False
        if pq.ctime_min is not None and r.ctime < pq.ctime_min:
            return False
        if pq.ctime_max is not None and r.ctime >= pq.ctime_max:
            return False
        if pq.exclude_exts and r.ext in pq.exclude_exts:
            return False
        return True

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, text: str, filters: FilterState = FilterState(), sort: SortSpec = SortSpec(),
                 favorites: Iterable[str] = ()):
        """``(order, total, root_counts, exact_order)``: ``order`` lists the ids in result order;
        ``exact_order`` is False when the engine is allowed to differ (tier-2 window)."""
        pq = parse(text, now=self.now)
        assert pq.error is None, pq.error
        if pq.is_empty() and filters.is_empty():
            return [], 0, (), True
        favkeys = frozenset(path_key(p) for p in favorites)
        pre = [r for r in self.rows if self._passes(pq, filters, r, favkeys) and self._text(pq, r)]
        rset = set(filters.root_ids)
        hits = [r for r in pre if not rset or r.root_id in rset]
        t4_pre: List[Row] = []
        q = None
        if (len(pq.positive_terms) == 1 and not pq.case_sensitive and not pq.any_groups and not pq.globs
                and not pq.path_terms and not pq.regex and not pq.negative_terms):
            q = pq.positive_terms[0]
            if not (2 <= len(q) <= 8 and q.isalnum()):
                q = None
        if q and len(hits) < 50:
            have = {r.id for r in pre}
            from fast_file_search.engine.textfold import initials
            t4_pre = [r for r in self.rows if r.id not in have and q in initials(r.name)
                      and self._passes(pq, filters, r, favkeys)]
        t4 = [r for r in t4_pre if not rset or r.root_id in rset]
        cnt = Counter(r.root_id for r in pre)
        cnt.update(r.root_id for r in t4_pre)
        root_counts = tuple((rid, cnt.get(rid, 0)) for rid in self.root_ids)
        exact = True
        if sort.key == "relevance":
            term = pq.first_term()
            if term and pq.case_sensitive:
                term = fold(term)

            def tier(r: Row) -> int:
                if not term:
                    return 3
                k = r.key
                if k.startswith(term):
                    lq = len(term)
                    if len(k) == lq or (k[lq] == "." and k.find(".", lq + 1) < 0):
                        return 0
                    return 1
                from fast_file_search.engine.textfold import boundary_mask
                bm = boundary_mask(r.name)
                p = k.find(term)
                while 0 <= p < 64:
                    if (bm >> p) & 1:
                        return 2
                    p = k.find(term, p + 1)
                return 3
            non_prefix = sum(1 for r in hits if term and not r.key.startswith(term))
            exact = non_prefix <= 10_000
            ordered = sorted(hits, key=lambda r: (tier(r), r.srank, r.id)) + sorted(t4, key=lambda r: (r.srank, r.id))
        else:
            desc = sort.descending
            if sort.key == "name":
                ordered = sorted(sorted(hits, key=lambda r: r.id), key=lambda r: r.key, reverse=desc)
            else:
                def val(r: Row):
                    if sort.key == "size":
                        return -1 if r.flags & FOLDER else r.size
                    if sort.key == "mtime":
                        return r.mtime
                    if sort.key == "folder":
                        return fold(r.dir_path)
                    if sort.key == "kind":
                        return r.ext
                    raise ValueError(sort.key)
                base = sorted(hits, key=lambda r: (r.key, r.id))
                ordered = sorted(base, key=val, reverse=desc)
            ordered += t4
        return [r.id for r in ordered], len(ordered), root_counts, exact


# ---------------------------------------------------------------------- fixtures

TRICKY = [
    "index.js", "Index.JS", "my_index_file.ts", "GiftTopIcon123.png", "giftTopIcon.svg", "README.md", "readme",
    "Résumé.pdf", "Résumé final.docx", "résumé.PDF", "Straße.txt", "STRASSE.log", "日本語ファイル.txt",
    "über_naïve.md", "Café.jpg", "café.jpeg", "50%_off.txt", "a_b%c.csv", "photo_a.jpg", "photo_b.png",
    "jquery.min.js", ".bashrc", ".env.local", "Makefile", "Dockerfile", "LICENSE", "CMakeLists.txt",
    "archive.tar.gz", "bundle.min.js", "UserProfile123.json", "report 2024 (final).xlsx", "x[1].txt", "tmp",
    "node_modules", "İstanbul.txt", "ıi.txt", "Ünïcödé.MD", "file with  two spaces.txt", "trailing.", "a.b.c",
    "bad\udcffname.txt", "\ud800lone.txt", "main.c", "main.cpp", "future.py",
]
STEMS = ["index", "main", "GiftTopIcon", "UserProfile", "README", "package", "config", "utils", "button",
         "Café", "résumé", "日本語", "über", "report", "invoice", "photo", "icon", "bundle", "chunk", "store",
         "test", "spec", "helper", "view", "model", "image", "Report", "data"]
EXTS = [".js", ".ts", ".json", ".md", ".png", ".jpg", ".txt", ".css", ".html", ".py", ".map", ".d.ts", ".svg",
        ".pdf", ".docx", ".csv", ".log", ".min.js", "", ".tmp", ".mp3", ".mp4", ".zip", ".app"]
DIRNAMES = ["src", "lib", "components", "assets", "icons", "node_modules", "docs", "build", "dist", "test",
            ".git", ".hidden", "Library", "tmp", "My Docs", "Ünïcode", "日本", "pkg", "vendor", "a.b"]


def gen_ops(seed: int, n_entries: int, roots: Sequence[str], start_id: int = 1, now: float = 1_750_000_000.0,
            alloc=None) -> List[tuple]:
    """A deterministic random tree as an op list (DIR before children; ids ascending)."""
    rnd = random.Random(seed)
    nid = [start_id]

    def new_id() -> int:
        if alloc is not None:
            return alloc()
        i = nid[0]
        nid[0] += 1
        return i

    ops: List[tuple] = []
    dirs: List[Tuple[int, int, str, int]] = []   # (dir_id, root_id, path, depth)
    for rp in roots:
        rid = new_id()
        ops.append((DIR, rid, 0, rid, rp, 0))
        dirs.append((rid, rid, rp, 0))
    names_in: Dict[int, Set[str]] = {}
    count = 0
    tricky = list(TRICKY)
    while count < n_entries:
        did, rid, dpath, depth = rnd.choice(dirs[-200:] if rnd.random() < 0.7 else dirs)
        used = names_in.setdefault(did, set())
        if rnd.random() < 0.12 and depth < 12:
            name = rnd.choice(DIRNAMES) + (str(rnd.randint(0, 30)) if rnd.random() < 0.5 else "")
            if name in used:
                continue
            used.add(name)
            i = new_id()
            flags = FOLDER | (HIDDEN if rnd.random() < 0.05 else 0)
            if name.endswith(".app") or rnd.random() < 0.02:
                flags |= PACKAGE
            mt = int(now - rnd.random() * 3 * 365 * 86400)
            ops.append((INS, i, did, name, 0, mt, 0, flags))
            path = _join(dpath, name)
            ops.append((DIR, i, did, rid, path, mt))
            dirs.append((i, rid, path, depth + 1))
        else:
            if tricky and rnd.random() < 0.3:
                name = tricky.pop(0)
            else:
                name = rnd.choice(STEMS) + (str(rnd.randint(0, 999)) if rnd.random() < 0.5 else "") + \
                    rnd.choice(EXTS)
            if name in used:
                continue
            used.add(name)
            i = new_id()
            size = int(rnd.expovariate(1 / 3000)) if rnd.random() < 0.97 else rnd.randint(10 ** 6, 5 * 10 ** 7)
            age = rnd.random() * 3 * 365 * 86400
            if name == "future.py":
                age = -10 * 86400
            mt = int(now - age)
            ct = int(mt - rnd.random() * 86400 * 30) if rnd.random() < 0.8 else 0
            flags = HIDDEN if rnd.random() < 0.03 else 0
            ops.append((INS, i, did, name, size, mt, ct, flags))
        count += 1
    return ops


def random_ops(rnd: random.Random, store, n: int, now: float) -> List[tuple]:
    """A random, valid mutation batch against the current store contents: inserts, updates,
    renames/moves (files and folders, with DIRPATH), deletes (files and folder subtrees)."""
    c = store.conn
    files = [r for r in c.execute("SELECT id, dir_id, name, flags FROM entries")]
    dirs = [r for r in c.execute("SELECT id, parent_id, root_id, path FROM dirs")]
    dirs_by_id = {d[0]: d for d in dirs}
    taken: Dict[int, Set[str]] = {}
    for i, d, name, f in files:
        taken.setdefault(d, set()).add(name)
    ops: List[tuple] = []
    gone: Set[int] = set()
    for _ in range(n):
        kind = rnd.random()
        if kind < 0.35 or not files:
            d = rnd.choice(dirs)
            name = rnd.choice(STEMS) + str(rnd.randint(0, 99999)) + rnd.choice(EXTS)
            if name in taken.setdefault(d[0], set()):
                continue
            taken[d[0]].add(name)
            i = store.allocate_ids(1)[0]
            ops.append((INS, i, d[0], name, rnd.randint(0, 10 ** 6), int(now - rnd.random() * 1e8), 0, 0))
        elif kind < 0.6:
            i, d, name, f = rnd.choice(files)
            if i in gone or i in dirs_by_id:
                continue
            ops.append((UPD, i, rnd.randint(0, 10 ** 7), int(now - rnd.random() * 1e8), 0, f & ~FOLDER))
        elif kind < 0.8:
            i, d, name, f = rnd.choice(files)
            if i in gone:
                continue
            target = rnd.choice(dirs) if rnd.random() < 0.5 else dirs_by_id.get(d)
            if target is None or target[0] in gone:
                continue
            if i in dirs_by_id:
                # never move a folder into its own subtree
                p = target
                bad = False
                while p is not None:
                    if p[0] == i:
                        bad = True
                        break
                    p = dirs_by_id.get(p[1])
                if bad or target[2] != dirs_by_id[i][2]:
                    continue
            new = "ren_" + str(rnd.randint(0, 99999)) + "_" + name
            if new in taken.setdefault(target[0], set()):
                continue
            taken[target[0]].add(new)
            taken.get(d, set()).discard(name)
            ops.append((REN, i, target[0], new))
            if i in dirs_by_id:
                newpath = _join(target[3], new)
                ops.append((DIRPATH, i, newpath))
                # stop here: later picks would need the updated paths
                break
        else:
            i, d, name, f = rnd.choice(files)
            if i in gone:
                continue
            ops.append((DEL, i))
            gone.add(i)
            if i in dirs_by_id:
                break   # a subtree vanished; keep the rest of the batch simple
    return ops
