"""P1 acceptance benchmark (design H.1) until P6's benchmarks/bench_engine.py exists.

Usage:  python tests/engine/bench_local.py [--tree DIR] [--scale 1|3] [--runs 5] [--no-ref] [--out x.json]

Indexes the diag tree (dist/diag/gen_tree.py output, 316k entries; default %TEMP%/ffs_diag_tree)
with a minimal scandir walker into a fresh Store, then measures every P1 acceptance number:
Store.apply and MemIndex.apply of 20k INS, DB bytes/entry, build from SQLite, snapshot load,
RSS, first-use mask builds, the search latency matrix (median of N runs per case), suggestions
overhead, history-only requests, cancellation latency, and exactness against reference.py.
All app paths are sandboxed to a temp dir; nothing outside it is written.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

SANDBOX = Path(tempfile.mkdtemp(prefix="ffs-p1-bench-"))
for _k in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
           "HOME", "USERPROFILE", "FAST_FILE_SEARCH_CACHE_DIR"):
    (SANDBOX / _k).mkdir()
    os.environ[_k] = str(SANDBOX / _k)

from fast_file_search.engine.memindex import MemIndex  # noqa: E402
from fast_file_search.engine.ops import DIR, INS  # noqa: E402
from fast_file_search.engine.protocol import FOLDER, HIDDEN, FilterState, SearchRequest, SortSpec  # noqa: E402
from fast_file_search.engine.store import Store  # noqa: E402
from fast_file_search.engine.userdata import HistoryItem  # noqa: E402

DAY = 86400


def rss_mb() -> float:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.WorkingSetSize / 1e6
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except OSError:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / 1e6 if sys.platform == "darwin" else r / 1e3


def walk_ops(root: str, store: Store, batch: int = 20_000):
    """Yield op batches (<= ``batch`` INS each) for a fresh scan of ``root`` (no excludes)."""
    rid = store.add_root(root)
    ops = [(DIR, rid, 0, rid, root, 0)]
    stack = [(rid, root)]
    n_ins = 0
    while stack:
        did, path = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            continue
        with it:
            ents = list(it)
        ids = store.allocate_ids(len(ents))
        for i, e in zip(ids, ents):
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            name = e.name
            isdir = e.is_dir(follow_symlinks=False)
            flags = (FOLDER if isdir else 0) | (HIDDEN if name.startswith(".") else 0)
            ct = int(getattr(st, "st_birthtime", 0) or 0)
            ops.append((INS, i, did, name, 0 if isdir else st.st_size, int(st.st_mtime), ct, flags))
            n_ins += 1
            if isdir:
                ops.append((DIR, i, did, rid, e.path, int(st.st_mtime)))
                stack.append((i, e.path))
            if n_ins >= batch:
                yield ops
                ops, n_ins = [], 0
    if ops:
        yield ops


def pct(vals, p):
    v = sorted(vals)
    return v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))] if v else 0.0


def timed(fn, runs):
    ts = []
    out = None
    for _ in range(runs):
        t0 = time.perf_counter()
        out = fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts), out


QUERIES = ["e", "i", "in", "ind", "index", "button", "gifttopicon", "userprofile123", "readme.md", "zzzqq", "日本",
           ".min.js", "index js", "component zz", "icon", "cafe"]
EXTRA = ["index OR button", "icon | photo json", "index -min", "NOT test icon", "*.min.js", "photo?.*", "gift*",
         "src/index", "components/", "re:^index\\d+\\.js$", "re:gift.*icon", "case:Index", "case:GiftTopIcon",
         '"index.js"', "résumé", "über", "ext:png icon", "type:image gift", "size:>10kb index", "date:30d"]


def specs(maxm):
    return {
        "none": FilterState(),
        "ext=js": FilterState(exts=frozenset({"js"})),
        "size<10KB": FilterState(size_max=10240),
        "date30d": FilterState(mtime_min=maxm - 30 * DAY),
        "path~components": FilterState(path_contains="components"),
        "excl nm,*.tmp": FilterState(excludes=("node_modules", "*.tmp")),
        "folders": FilterState(kinds=frozenset({"folder"})),
        "ALL": FilterState(exts=frozenset({"js", "ts", "css"}), size_max=65536, mtime_min=maxm - 365 * DAY,
                           path_contains="src", excludes=("node_modules", "*.tmp")),
    }


def replicate(src: Store, dst: Store, k: int):
    """dst = src replicated k times (ids offset, root paths suffixed) — the '950k' scale."""
    rows = [r for ch in src.iter_load() for r in ch.entries]
    dirs = [d for ch in src.iter_load() for d in ch.dirs]
    off = max(max(r[0] for r in rows), max(d[0] for d in dirs)) + 1
    for c in range(k):
        o = c * off
        sfx = f"~{c}" if c else ""
        ops = []
        for d in sorted(dirs, key=lambda d: d[0]):
            root_path = next(x[3] for x in dirs if x[0] == d[2])
            path = root_path + sfx + d[3][len(root_path):]
            ops.append((DIR, d[0] + o, d[1] + o if d[1] else 0, d[2] + o, path, d[4]))
        dst.apply(ops)
        for i in range(0, len(rows), 50_000):
            dst.apply([(INS, r[0] + o, r[1] + o, r[2], r[3], r[4], r[5], r[6]) for r in rows[i:i + 50_000]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", default=os.path.join(tempfile.gettempdir(), "ffs_diag_tree"))
    ap.add_argument("--scale", type=int, default=1)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--no-ref", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    # One fixed "now" for the whole run: relative dates (date:30d) and age buckets must not move
    # between the engine and the reference evaluation.
    frozen = time.time()
    MemIndex.clock = staticmethod(lambda: frozen)
    res = {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
           "scale": args.scale}
    out = SANDBOX / "cache"
    out.mkdir()

    # ---------------------------------------------------------------- index the tree
    store = Store(out / "index-v5.db")
    store.set_fast_writes(True)
    apply_ms, mem_apply_ms = [], []
    live = MemIndex()
    t0 = time.perf_counter()
    for ops in walk_ops(args.tree, store):
        n_ins = sum(1 for o in ops if o[0] == INS)
        t1 = time.perf_counter()
        g = store.apply(ops)
        t2 = time.perf_counter()
        live.apply(ops, g)
        t3 = time.perf_counter()
        if n_ins >= 19_000:
            apply_ms.append((t2 - t1) * 1000)
            mem_apply_ms.append((t3 - t2) * 1000)
    walk_s = time.perf_counter() - t0
    store.set_fast_writes(False)
    n_entries = store.counts()[0]
    if args.scale > 1:
        big = Store(out / "big.db")
        big.set_fast_writes(True)
        replicate(store, big, args.scale)
        store.close()
        store = big
        n_entries = store.counts()[0]
        live = None
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db_bytes = os.path.getsize(store.path)
    res.update(entries=n_entries, walk_write_s=round(walk_s, 2), entries_per_s=round(n_entries / walk_s),
               store_apply_20k_ms_max=round(max(apply_ms or [0]), 1),
               store_apply_20k_ms_median=round(statistics.median(apply_ms or [0]), 1),
               memindex_apply_20k_ms_max=round(max(mem_apply_ms or [0]), 1),
               db_bytes_per_entry=round(db_bytes / n_entries, 1))
    del live
    gc.collect()

    # ---------------------------------------------------------------- build / snapshot / RSS
    rss0 = rss_mb()
    t0 = time.perf_counter()
    m = MemIndex.build(store)
    res["build_s"] = round(time.perf_counter() - t0, 3)
    gc.collect()
    res["rss_index_mb"] = round(rss_mb() - rss0, 1)
    snap = out / "index-v5.snap"
    t0 = time.perf_counter()
    m.save_snapshot(snap)
    res["snapshot_save_s"] = round(time.perf_counter() - t0, 3)
    res["snapshot_mb"] = round(os.path.getsize(snap) / 1e6, 1)
    loads = []
    for _ in range(3):
        t0 = time.perf_counter()
        m2 = MemIndex.load_snapshot(snap, store.generation)
        loads.append(time.perf_counter() - t0)
        assert m2 is not None
        del m2
        gc.collect()
    res["snapshot_load_s"] = round(min(loads), 3)

    maxm = max(m.mtime)
    sp = specs(maxm)
    never = lambda: False  # noqa: E731

    # ---------------------------------------------------------------- first-use masks
    first = {}
    for name, fs in sp.items():
        if fs.is_empty() or name == "ALL":
            continue
        m._masks.clear()
        m._dir_masks.clear()
        comps = m._components(__import__("fast_file_search.engine.query", fromlist=["parse"]).parse(""), fs, None)
        t0 = time.perf_counter()
        for c in comps:
            m._mask(c)
        first[name] = round((time.perf_counter() - t0) * 1000, 1)
    res["first_use_mask_ms"] = first
    m._masks.clear()
    m._dir_masks.clear()

    # ---------------------------------------------------------------- latency matrix
    gen = [0]

    def run(text, fs=FilterState(), sort=SortSpec(), sugg=False, user=None):
        gen[0] += 1
        return m.search(SearchRequest(gen[0], text, fs, sort, suggestions=sugg), never, user)

    for name, fs in sp.items():   # warm masks (steady state = cached masks)
        run("e", fs)
    cases = []
    for name, fs in sp.items():
        for q in QUERIES + [""]:
            if not q and fs.is_empty():
                continue
            ms, r = timed(lambda: run(q, fs), args.runs)
            cases.append({"q": q, "spec": name, "ms": round(ms, 2), "total": r.total, "kind": "matrix"})
    for q in EXTRA:
        ms, r = timed(lambda: run(q), args.runs)
        cases.append({"q": q, "spec": "none", "ms": round(ms, 2), "total": r.total, "kind": "extra"})
    for key in ("name", "size", "mtime", "folder", "kind"):
        for q in ("e", "index"):
            for desc in (False, True):
                ms, r = timed(lambda: run(q, sort=SortSpec(key, desc)), args.runs)
                cases.append({"q": q, "spec": f"sort={key}{'-' if desc else '+'}", "ms": round(ms, 2),
                              "total": r.total, "kind": "sort"})
    allms = [c["ms"] for c in cases]
    long_ = [c["ms"] for c in cases if len(c["q"].replace(" ", "")) >= 3]
    fonly = [c["ms"] for c in cases if c["q"] == ""]
    res["search"] = {"cases": len(cases), "p50": pct(allms, .5), "p95": pct(allms, .95), "max": max(allms),
                     "ge3_p95": pct(long_, .95), "ge3_max": max(long_), "filter_only_max": max(fonly),
                     "slowest": sorted(cases, key=lambda c: -c["ms"])[:8]}

    # ---------------------------------------------------------------- suggestions
    hist = [HistoryItem(q, 1, i < 2, float(i)) for i, q in enumerate(
        ["index", "index js", "icon", "gifttopicon", "readme", "button", "invoice", "image", "in", "main"])]

    class U:
        favorites_generation = 0

        def favorite_paths(self):
            return frozenset()

        def history(self, prefix="", limit=20):
            from fast_file_search.engine.userdata import matches_prefix
            return [h for h in hist if matches_prefix(h.query, prefix)][:limit]
    user = U()
    deltas = []
    for name, fs in sp.items():
        for q in QUERIES:
            a, _ = timed(lambda: run(q, fs, sugg=False, user=user), args.runs)
            b, _ = timed(lambda: run(q, fs, sugg=True, user=user), args.runs)
            deltas.append(b - a)
    h_only, _ = timed(lambda: run("", sugg=True, user=user), args.runs)
    h_only_f, _ = timed(lambda: run("", sp["ALL"], sugg=True, user=user), args.runs)
    res["suggestions"] = {"delta_p95_ms": round(pct(deltas, .95), 2), "delta_max_ms": round(max(deltas), 2),
                          "history_only_ms": round(h_only, 2), "history_only_filtered_ms": round(h_only_f, 2)}

    # ---------------------------------------------------------------- append-only: masks extended
    before = dict(m.stats)
    rid = next(iter(m.root_slot))
    ids = store.allocate_ids(20_000)
    ops = [(INS, i, rid, f"bench_append_{i}.js", 100, int(maxm), 0, 0) for i in ids]
    t0 = time.perf_counter()
    m.apply(ops, store.generation + 1)
    res["memindex_apply_20k_ins_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    for fs in sp.values():
        run("index", fs)
    res["masks_after_append"] = {"built": m.stats["mask_build"] - before.get("mask_build", 0),
                                 "extended": m.stats["mask_extend"] - before.get("mask_extend", 0)}

    # ---------------------------------------------------------------- cancellation
    run("e", sp["size<10KB"])   # absorb the one-off work after the append above (not cancellation)
    lat = []
    for delay in (0.004, 0.009, 0.015, 0.022, 0.03, 0.037, 0.045):
        flag = {"t": None}

        def flip():
            time.sleep(delay)
            flag["t"] = time.perf_counter()
        th = threading.Thread(target=flip)
        th.start()
        gen[0] += 1
        r = m.search(SearchRequest(gen[0], "e", sp["size<10KB"]), lambda: flag["t"] is not None, None)
        t_end = time.perf_counter()
        th.join()
        if r is None and flag["t"] is not None:
            lat.append((t_end - flag["t"]) * 1000)
    res["cancel_ms_max"] = round(max(lat), 2) if lat else None
    res["cancel_samples"] = len(lat)

    # ---------------------------------------------------------------- exactness
    if not args.no_ref and args.scale == 1:
        from reference import Reference
        # the appended rows are in memory only: rebuild from the store for a clean comparison
        m = MemIndex.build(store)
        ref = Reference.from_store(store, MemIndex.clock())
        bad = []
        checked = 0
        texts = QUERIES + EXTRA + [""]
        for name, fs in sp.items():
            for q in texts:
                if not q and fs.is_empty():
                    continue
                order, total, rc, exact = ref.evaluate(q, fs)
                gen[0] += 1
                r = m.search(SearchRequest(gen[0], q, fs, limit=200), never, None)
                ids = [x.id for x in r.rows]
                if r.total > 200:
                    ids += [x.id for x in m.page(gen[0], 200, r.total).rows]
                checked += 1
                ok = r.total == total and set(ids) == set(order) and (not exact or ids == order)
                again = m.search(SearchRequest(gen[0] + 1, q, fs, limit=200), never, None)
                ok = ok and [x.id for x in again.rows] == ids[:200]
                if not ok:
                    bad.append((q, name, r.total, total))
        for key in ("name", "size", "mtime", "folder", "kind"):
            for q in ("e", "index", "gift*"):
                order, total, rc, exact = ref.evaluate(q, FilterState(), SortSpec(key, True))
                gen[0] += 1
                r = m.search(SearchRequest(gen[0], q, sort=SortSpec(key, True), limit=200), never, None)
                checked += 1
                if r.total != total or [x.id for x in r.rows] != order[:200]:
                    bad.append((q, key, r.total, total))
        res["exactness"] = {"checked": checked, "mismatches": bad}
    store.close()
    text = json.dumps(res, indent=1, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil
        shutil.rmtree(SANDBOX, ignore_errors=True)
