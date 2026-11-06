"""In-memory index (design A.4-A.10, E.1) against the brute-force reference (``reference.py``)."""
from __future__ import annotations

import os
import random
import threading
import time
from pathlib import Path

import pytest

import fast_file_search.engine.memindex as mi
from fast_file_search.engine import snapshot
from fast_file_search.engine.memindex import IndexCounters, MemIndex
from fast_file_search.engine.ops import DEL, DIR, DIRPATH, INS, REN, UPD
from fast_file_search.engine.protocol import FOLDER, FilterState, SearchRequest, SortSpec
from fast_file_search.engine.store import Store
from fast_file_search.engine.userdata import HistoryItem

from reference import SEP, Reference, gen_ops, random_ops

NOW = 1_750_000_000.0
DAY = 86_400
BASE = "C:\\fx" if os.name == "nt" else "/fx"
ROOTS = [BASE + SEP + "alpha", BASE + SEP + "Beta Root"]
NEVER = lambda: False  # noqa: E731


class FakeUser:
    def __init__(self, favorites=(), history=()):
        self._favs = frozenset(favorites)
        self.favorites_generation = 1
        self._hist = list(history)

    def favorite_paths(self):
        return self._favs

    def history(self, prefix="", limit=20):
        from fast_file_search.engine.userdata import matches_prefix
        return [h for h in self._hist if matches_prefix(h.query, prefix)][:limit]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(MemIndex, "clock", staticmethod(lambda: NOW))


def build_world(tmp: Path, seed: int, n: int, batch: int = 700):
    """Store + a live MemIndex fed batch by batch (like the writer) + a reference."""
    store = Store(tmp / f"w{seed}.db")
    live = MemIndex()
    ops = gen_ops(seed, n, ROOTS, alloc=lambda: store.allocate_ids(1)[0])
    for i in range(0, len(ops), batch):
        chunk = ops[i:i + batch]
        g = store.apply(chunk)
        live.apply(chunk, g)
    return store, live


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(MemIndex, "clock", staticmethod(lambda: NOW))
    tmp = tmp_path_factory.mktemp("world")
    store, live = build_world(tmp, 11, 5000)
    ref = Reference.from_store(store, NOW)
    built = MemIndex.build(store)
    yield store, live, built, ref
    store.close()
    mp.undo()


def engine_all(m: MemIndex, text, filters=FilterState(), sort=SortSpec(), user=None, limit=40, gen=[0]):
    gen[0] += 1
    g = gen[0]
    r = m.search(SearchRequest(g, text, filters, sort, limit=limit), NEVER, user)
    assert r is not None and r.error is None, (text, r and r.error)
    ids = [row.id for row in r.rows]
    if r.total > len(ids):
        off = len(ids)
        while off < r.total:          # uneven page sizes on purpose
            p = m.page(g, off, 333)
            assert p is not None and p.offset == off and p.total == r.total
            ids += [row.id for row in p.rows]
            if not p.rows:
                break
            off += len(p.rows)
    return r, ids


def check(m: MemIndex, ref: Reference, text, filters=FilterState(), sort=SortSpec(), favorites=()):
    user = FakeUser(favorites)
    r, ids = engine_all(m, text, filters, sort, user)
    order, total, rc, exact = ref.evaluate(text, filters, sort, favorites)
    ctx = (text, filters, sort)
    assert r.total == total, ctx
    assert len(ids) == len(set(ids)) == total, ctx
    assert set(ids) == set(order), ctx
    if filters.root_ids or text or not filters.is_empty():
        assert dict(r.root_counts) == dict(rc), ctx
    if exact:
        assert ids == order, ctx
    return r, ids


# ------------------------------------------------------------------------------- matrix

TEXTS = [
    "e", "in", "index", "INDEX", "resume", "résumé", "Re\u0301sume\u0301", "strasse", "straße", "日本", "cafe",
    "gti", "up1", "index js", "component zz", "icon", "photo OR readme", "photo | invoice json", "-index",
    "index -min", "NOT test spec", "*.min.js", "photo_?.*", "report*", "*report*", "*", "src/index", "src/",
    "/index", "components/*.ts", '"two spaces"', '"file with  two"', "50%", "%", "_off", "a_b%", "x[1]",
    r"re:^main\.(c|cpp)$", r"re:\d{3}", "re:café", "re:^\\.", "case:README", "case:Résumé", "case:readme",
    "case:Index", "ext:js", "ext:pdf,docx", "ext:!js index", "-ext:md readme", "type:image", "type:code,document",
    "kind:folder", "type:file icon", "type:folder src", "size:>10kb", "size:1kb..5kb", "size:<100", "size:empty",
    "date:2024", "modified:>2025-01-01", "date:30d", "date:>1y", "created:2024", "path:components",
    "path:src index", "path:alpha/src", "name:index", "a.b", ".bashrc", ".env", "trailing.", "İstanbul",
    "istanbul", "ıi", "makefile", "license", "bad", "lone", "\ufffd", "ünïcödé", "unicode", "ü", "@", "tmp",
    "node_modules", "ab", "zzzqq", "future", "main.c", "a.b.c", "ext:app", "type:archive", "uuuuuuuuuuu",
    "beta root/", "alpha/docs/", "icon OR main -js", "ren_",
]


def specs(ref: Reference):
    root_a = ref.root_ids[0]
    some_dir = next(r.dir_path for r in ref.rows if len(r.comps) == 2)
    favs = [r.path for r in ref.rows[::97]]
    return {
        "none": (FilterState(), ()),
        "folders": (FilterState(kinds=frozenset({"folder"})), ()),
        "images+code": (FilterState(kinds=frozenset({"image", "code"})), ()),
        "ext js,ts": (FilterState(exts=frozenset({"js", "TS", ".json"})), ()),
        "size<10KB": (FilterState(size_max=10 * 1024), ()),
        "size 1-100KB": (FilterState(size_min=1024, size_max=100 * 1024), ()),
        "year": (FilterState(mtime_min=NOW - 365 * DAY), ()),
        "old": (FilterState(mtime_max=NOW - 400.5 * DAY), ()),
        "root A": (FilterState(root_ids=frozenset({root_a})), ()),
        "folder scope": (FilterState(folder=some_dir), ()),
        "path~src": (FilterState(path_contains="SRC"), ()),
        "excl": (FilterState(excludes=("node_modules", "*.tmp", ".git")), ()),
        "excl seq": (FilterState(excludes=("src/components", "a.b", "*.min.js", "x[1].txt", "tmp?")), ()),
        "favorites": (FilterState(favorites_only=True), favs),
        "ALL": (FilterState(kinds=frozenset({"code", "document", "folder"}), size_max=65536,
                            mtime_min=NOW - 3 * 365 * DAY, path_contains="s", excludes=("node_modules", "*.tmp"),
                            root_ids=frozenset({root_a})), ()),
    }


@pytest.mark.parametrize("which", ["live", "built"])
def test_text_matrix_is_exact(world, which):
    store, live, built, ref = world
    m = live if which == "live" else built
    for text in TEXTS:
        check(m, ref, text)


def test_filter_matrix_is_exact(world):
    store, live, built, ref = world
    texts = ["", "e", "index", "icon", "-index", "*.js", "src/", "photo OR readme", r"re:\d", "case:Index", "gti",
             "ext:md", "type:image"]
    for name, (fs, favs) in specs(ref).items():
        for text in texts:
            check(live, ref, text, fs, favorites=favs)


def test_filters_only_global_order_walk_is_exact(world, monkeypatch):
    store, live, built, ref = world
    monkeypatch.setattr(mi, "WALK_ABOVE", 0)
    for name, (fs, favs) in specs(ref).items():
        if not fs.is_empty():
            check(built, ref, "", fs, favorites=favs)
            check(built, ref, "ext:md", fs, favorites=favs)


def test_global_sort_order_walk_is_exact(world, monkeypatch):
    store, live, built, ref = world
    monkeypatch.setattr(mi, "FULL_SORT_BELOW", 0)
    before = built.stats["sort_order_build"]
    for key in ("name", "size", "mtime", "folder", "kind"):
        for desc in (False, True):
            for text in ["e", "index", "*.js", ""]:
                fs = FilterState(kinds=frozenset({"file", "folder"})) if text == "" else FilterState()
                check(built, ref, text, fs, SortSpec(key, desc))
    assert built.stats["sort_order_build"] > before


REGEXES = [r"re:\d{3}", "re:^main", "re:s$", "re:^.$", "re:[^a-z]x", r"re:\bin", r"re:\w+\.js$", "re:a*",
           "re:(?i)CAFE", r"re:\s", "re:x|^ind", "re:ICON", "re:.min.", "case:re:^I", "case:re:Index"]


def test_regex_blob_and_per_key_paths_agree_with_reference(world):
    store, live, built, ref = world
    for text in REGEXES:
        check(live, ref, text)
        check(live, ref, text, FilterState(size_max=5000))


def test_sorts_are_exact_and_deterministic(world):
    store, live, built, ref = world
    sorts = [SortSpec("name"), SortSpec("name", True), SortSpec("size"), SortSpec("size", True),
             SortSpec("mtime"), SortSpec("mtime", True), SortSpec("folder"), SortSpec("folder", True),
             SortSpec("kind"), SortSpec("kind", True), SortSpec("relevance")]
    for sort in sorts:
        for text in ["", "e", "index", "icon", "photo OR readme", "gti", "*.js"]:
            fs = FilterState(kinds=frozenset({"file", "folder"})) if text == "" else FilterState()
            r1, ids1 = check(live, ref, text, fs, sort)
            r2, ids2 = engine_all(built, text, fs, sort)
            assert ids1 == ids2, (text, sort)


def test_multi_segment_scans_agree(world, monkeypatch, tmp_path):
    """The segment boundaries (find loop, offsets, cancellation chunks) must not matter."""
    monkeypatch.setattr(mi, "SEGMENT", 512)
    store, live, built, ref = world
    small = MemIndex.build(store)
    assert len(small._segs) > 5
    for text in ["e", "index", "gti", "*.min.js", r"re:\d{3}", "src/index", "photo OR readme", "-e", "zzzqq"]:
        check(small, ref, text)
        check(small, ref, text, FilterState(size_max=5000))
    # appends into a partially filled tail segment, then more segments
    g = store.generation
    extra = [(INS, i, ref.root_ids[0], f"seg_extra_{i}.txt", 1, int(NOW), 0, 0) for i in store.allocate_ids(1500)]
    g = store.apply(extra)
    small.apply(extra, g)
    ref2 = Reference.from_store(store, NOW)
    check(small, ref2, "seg_extra")
    check(small, ref2, "e")
    # undo for the other tests sharing the module world
    dels = [(DEL, o[1]) for o in extra]
    g = store.apply(dels)
    live.apply(dels, g)
    built.apply(dels, g)


# ------------------------------------------------------------------------------- randomized

POOL = ["index", "e", "in", "icon", "photo", "report", "résumé", "cafe", "日本", "main", "js", "md", "src",
        "-test", "-min", "!json", "*.js", "*.md", "photo_?.*", "src/", "/index", "OR", "|", "ext:js", "ext:md,png",
        "type:image", "type:folder", "type:code", "size:>2kb", "size:<500", "date:>2024", "date:6m",
        "path:src", "path:icons", '"index.js"', "gti", "case:", "a", "b", "re:^ma"]


def random_query(rnd: random.Random) -> str:
    toks = [rnd.choice(POOL) for _ in range(rnd.randint(1, 4))]
    if "case:" in toks:
        toks.remove("case:")
        toks.insert(0, "case:")
        return "case:" + " ".join(toks[1:])
    if any(t.startswith("re:") for t in toks):
        t = next(t for t in toks if t.startswith("re:"))
        toks.remove(t)
        toks.append(t)
    return " ".join(toks)


def random_filters(rnd: random.Random, ref: Reference) -> FilterState:
    kw = {}
    if rnd.random() < 0.3:
        kw["kinds"] = frozenset(rnd.sample(["folder", "file", "image", "document", "code", "audio", "archive"],
                                           rnd.randint(1, 2)))
    if rnd.random() < 0.2:
        kw["exts"] = frozenset(rnd.sample(["js", "md", "png", "json", "pdf", "txt"], rnd.randint(1, 3)))
    if rnd.random() < 0.25:
        kw["size_min"] = rnd.choice([None, 0, 100, 2048])
        kw["size_max"] = rnd.choice([None, 1000, 50_000])
    if rnd.random() < 0.25:
        kw["mtime_min"] = NOW - rnd.randint(1, 1000) * DAY
    if rnd.random() < 0.15:
        kw["root_ids"] = frozenset({rnd.choice(ref.root_ids)})
    if rnd.random() < 0.15:
        kw["path_contains"] = rnd.choice(["src", "lib", "a.b", "docs/", "日本"])
    if rnd.random() < 0.2:
        kw["excludes"] = tuple(rnd.sample(["node_modules", "*.tmp", ".git", "src/lib", "*.js", "test*"],
                                          rnd.randint(1, 3)))
    if rnd.random() < 0.1:
        kw["folder"] = rnd.choice([r.dir_path for r in ref.rows[:500]])
    return FilterState(**kw)


def test_randomized_against_reference(world):
    store, live, built, ref = world
    for seed in range(200):
        rnd = random.Random(seed)
        text = random_query(rnd)
        fs = random_filters(rnd, ref)
        sort = rnd.choice([SortSpec(), SortSpec(), SortSpec("name"), SortSpec("size", True), SortSpec("mtime")])
        from fast_file_search.engine.query import parse
        if parse(text, now=NOW).error:
            continue
        check(live if seed % 2 else built, ref, text, fs, sort)


# ------------------------------------------------------------------------------- consistency

def dump(m: MemIndex):
    out = {}
    for s in range(m.n):
        if m.live[s]:
            out[m.ids[s]] = (m.path_of(m.ids[s]), m.size[s], m.mtime[s], m.ctime[s], m.flags[s], m.keys[s],
                             m.ext_names[m.ext[s]], m.srank[s] >> mi.SLOT_BITS)
    return out


def test_build_from_store_equals_live_index(world):
    store, live, built, ref = world
    assert dump(built) == dump(live)
    assert built.counters() == live.counters()
    assert built.generation == live.generation == store.generation


def test_random_op_stream_keeps_store_and_index_identical(tmp_path):
    store, live = build_world(tmp_path, 3, 1500, batch=300)
    rnd = random.Random(42)
    for step in range(60):
        ops = random_ops(rnd, store, rnd.randint(1, 40), NOW)
        if not ops:
            continue
        g = store.apply(ops)
        live.apply(ops, g)
        if step % 10 == 0:
            ref = Reference.from_store(store, NOW)
            for text in ["e", "ren_", "index", "-e", "src/"]:
                check(live, ref, text)
    rebuilt = MemIndex.build(store)
    assert dump(rebuilt) == dump(live)
    # regression (P2): save_snapshot after an arbitrary apply sequence, with lazily computed
    # columns only partly filled in, must work and load back identical
    live.search(SearchRequest(1, "ren"), NEVER, None)          # computes some boundaries/initials
    snap = tmp_path / "after_ops.snap"
    live.save_snapshot(snap)
    back = MemIndex.load_snapshot(snap, live.generation)
    assert back is not None and dump(back) == dump(live)
    assert results(back) == results(live)
    assert rebuilt.counters()[:4] == live.counters()[:4]
    ref = Reference.from_store(store, NOW)
    for text in TEXTS[:40]:
        check(live, ref, text)
    # compaction drops tombstones and keeps every answer
    assert live.dead > 0
    epoch = live.epoch
    assert live.compact_if_needed(force=True)
    assert live.dead == 0 and live.epoch == epoch + 1
    assert dump(live) == dump(rebuilt)
    for text in TEXTS[:40]:
        check(live, ref, text)
    store.close()


def test_counters_match_reference(world):
    store, live, built, ref = world
    c = live.counters()
    assert isinstance(c, IndexCounters)
    assert c.total_entries == len(ref.rows)
    assert c.total_folders == sum(1 for r in ref.rows if r.flags & FOLDER)
    assert c.total_bytes == sum(r.size for r in ref.rows if not r.flags & FOLDER)
    for rid in ref.root_ids:
        rows = [r for r in ref.rows if r.root_id == rid]
        rc = c.roots[rid]
        assert (rc.entries, rc.folders) == (len(rows), sum(1 for r in rows if r.flags & FOLDER))
    from collections import Counter
    exts = Counter(r.ext for r in ref.rows if not r.flags & FOLDER and r.ext)
    top = dict(c.top_exts)
    assert len(top) == min(20, len(exts))
    assert all(exts[e] == n for e, n in top.items())
    assert min(top.values()) >= sorted(exts.values(), reverse=True)[len(top) - 1]


def test_path_of_and_ids_for_paths(world):
    store, live, built, ref = world
    sample = ref.rows[::250]
    for r in sample:
        assert live.path_of(r.id) == r.path
    assert live.path_of(10 ** 12) is None
    got = live.ids_for_paths([r.path for r in sample] + [BASE + SEP + "missing.txt"])
    assert got == [r.id for r in sample]
    if os.name == "nt":
        assert live.ids_for_paths([sample[0].path.upper().replace("\\", "/")]) == [sample[0].id]


# ------------------------------------------------------------------------------- masks

def test_masks_are_extended_not_rebuilt_after_appends(tmp_path):
    store, live = build_world(tmp_path, 5, 3000)
    fs = FilterState(kinds=frozenset({"code"}), size_max=50_000, mtime_min=NOW - 400 * DAY,
                     excludes=("node_modules", "*.tmp"), path_contains="s")
    live.search(SearchRequest(1, "e", fs), NEVER, None)
    built_before = live.stats["mask_build"]
    ext_before = live.stats["mask_extend"]
    rid = next(iter(live.root_slot))
    for k in range(3):
        ids = store.allocate_ids(200)
        ops = [(INS, i, rid, f"appended_{k}_{i}.py", 10, int(NOW), 0, 0) for i in ids]
        live.apply(ops, store.apply(ops))
        r = live.search(SearchRequest(2 + k, "e", fs), NEVER, None)
        assert r.total > 0
    assert live.stats["mask_build"] == built_before            # nothing rebuilt
    assert live.stats["mask_extend"] >= ext_before + 3 * 5     # every component extended each time
    ref = Reference.from_store(store, NOW)
    check(live, ref, "e", fs)
    check(live, ref, "appended", fs)
    # an in-place change patches (or drops) the affected masks; answers stay exact
    some = next(r for r in ref.rows if not r.flags & FOLDER)
    ops = [(UPD, some.id, 10 ** 9, int(NOW), 0, 0)]
    live.apply(ops, store.apply(ops))
    check(live, Reference.from_store(store, NOW), "e", fs)
    store.close()


# ------------------------------------------------------------------------------- ranking

def tiny(tmp_path, names, root=None, folders=()):
    """A one-root index with ``names`` (files) and ``folders`` (dir names, each with no children)."""
    store = Store(tmp_path / "tiny.db")
    root = root or ROOTS[0]
    rid = store.add_root(root)
    ops = [(DIR, rid, 0, rid, root, 0)]
    dir_ids = {}
    for nm in folders:
        i = store.allocate_ids(1)[0]
        dir_ids[nm] = i
        ops += [(INS, i, rid, nm, 0, int(NOW), 0, FOLDER), (DIR, i, rid, rid, root + SEP + nm, 0)]
    for item in names:
        nm, d, size, age = (item + (None, 1, 0))[:4] if isinstance(item, tuple) else (item, None, 1, 0)
        parent = rid if d is None else dir_ids[d]
        i = store.allocate_ids(1)[0]
        ops.append((INS, i, parent, nm, size, int(NOW - age * DAY), 0, 0))
    m = MemIndex()
    m.apply(ops, store.apply(ops))
    return store, m


def names_of(m, text, **kw):
    r = m.search(SearchRequest(1, text, **kw), NEVER, None)
    return [row.name for row in r.rows]


def test_relevance_tiers(tmp_path):
    store, m = tiny(tmp_path, ["bicon.txt", "myIcon.ts", "my-icon.svg", "iconic.js", "icon.png", "icon",
                               "ICON.tar", "zicon_x"])
    got = names_of(m, "icon")
    # exact (name or name minus last extension), then prefix, then word/camelCase boundary, then substring
    assert got[:3] == ["icon", "ICON.tar", "icon.png"] or set(got[:3]) == {"icon", "ICON.tar", "icon.png"}
    assert got[3] == "iconic.js"
    assert set(got[4:6]) == {"myIcon.ts", "my-icon.svg"}
    assert set(got[6:]) == {"bicon.txt", "zicon_x"}
    store.close()


def test_static_rank_tie_breaks(tmp_path):
    store, m = tiny(tmp_path, [("report.txt", "deep", 1, 0), "report_long_name.txt", ".report", "report.md",
                               ("report.pdf", None, 1, 400)], folders=["deep", "report"])
    got = names_of(m, "report")
    # exact matches first: shallower beats deeper, a folder beats a file of equal length, recent beats old,
    # dotfiles (hidden) last
    assert got.index("report") < got.index("report.md") < got.index("report.pdf")
    assert got.index("report.md") < got.index("report.txt")                 # depth 0 before depth 1
    assert got.index("report_long_name.txt") < got.index(".report")
    store.close()


def test_abbreviation_tier_only_for_small_result_sets(tmp_path):
    names = ["GiftTopIcon.png", "gti_notes.txt", "GreatTeamIdea.md", "other.txt"]
    store, m = tiny(tmp_path, names)
    r = m.search(SearchRequest(1, "gti"), NEVER, None)
    got = [row.name for row in r.rows]
    assert got[0] == "gti_notes.txt"                    # real substring hit first
    assert set(got[1:]) == {"GiftTopIcon.png", "GreatTeamIdea.md"} and r.total == 3
    assert r.rows[1].spans and all(b - a == 1 for a, b in r.rows[1].spans)
    store.close()
    store, m = tiny(tmp_path / "x", [f"gti_{i}.txt" for i in range(60)] + ["GiftTopIcon.png"])
    r = m.search(SearchRequest(1, "gti"), NEVER, None)
    assert r.total == 60                                # >= 50 substring hits: no abbreviations
    store.close()


def test_spans_and_rows(tmp_path):
    store, m = tiny(tmp_path, [("Résumé final.PDF", "Docs", 1234, 0)], folders=["Docs"])
    r = m.search(SearchRequest(1, "resume pdf"), NEVER, None)
    row = r.rows[0]
    assert row.name == "Résumé final.PDF" and row.spans == ((0, 6), (13, 16))
    assert row.path == ROOTS[0] + SEP + "Docs" + SEP + "Résumé final.PDF"
    assert row.rel_dir == "Docs" and row.size == 1234 and not row.favorite
    assert r.terms == ("resume", "pdf")
    r = m.search(SearchRequest(2, "re:sum.*fin"), NEVER, None)
    assert r.rows[0].spans == ((2, 10),)
    store.close()


# ------------------------------------------------------------------------------- v1 behaviours

V1 = [("/docs", "annual_report_2024.pdf", 2 * 1024 * 1024, 1_700_000_000),
      ("/docs", "draft_notes.txt", 5 * 1024, 1_710_000_000),
      ("/docs/my docs", "final_draft.txt", 50, 1_720_000_000),
      ("/code", "main.c", 300, int(NOW)), ("/code", "main.cpp", 400, int(NOW)),
      ("/code", "Readme.md", 1000, int(NOW)), ("/code", "README.txt", 1000, int(NOW)),
      ("/code", "future.py", 10, int(NOW) + 10 * DAY),
      ("/pics", "photo_a.jpg", 3 * 1024 * 1024, 1_600_000_000), ("/pics", "photo_b.png", 4 * 1024 * 1024, 1_650_000_000),
      ("/odd", "50%_off_my_file.txt", 5, 1_700_000_000)]


@pytest.fixture
def v1(tmp_path):
    store = Store(tmp_path / "v1.db")
    root = BASE + SEP + "v1"
    rid = store.add_root(root)
    ops = [(DIR, rid, 0, rid, root, 0)]
    dirs = {"": rid}
    for d, name, size, mt in V1:
        parts = [p for p in d.split("/") if p]
        cur = ""
        for p in parts:
            nxt = cur + "/" + p
            if nxt not in dirs:
                i = store.allocate_ids(1)[0]
                ops += [(INS, i, dirs[cur], p, 0, 0, 0, FOLDER),
                        (DIR, i, dirs[cur], rid, root + SEP + SEP.join(nxt.strip("/").split("/")), 0)]
                dirs[nxt] = i
            cur = nxt
        ops.append((INS, store.allocate_ids(1)[0], dirs[cur], name, size, mt, mt, 0))
    m = MemIndex()
    m.apply(ops, store.apply(ops))
    yield m
    store.close()


def files_of(m, text, **kw):
    r = m.search(SearchRequest(1, text, **kw), NEVER, None)
    assert r.error is None, r.error
    return sorted(row.name for row in r.rows if not row.flags & FOLDER)


def test_v1_terms_or_not_wildcards(v1):
    assert files_of(v1, "draft notes") == ["draft_notes.txt"]
    assert files_of(v1, "photo_a OR photo_b") == ["photo_a.jpg", "photo_b.png"]
    assert files_of(v1, "draft NOT final") == ["draft_notes.txt"]
    assert files_of(v1, "draft AND final") == ["final_draft.txt"]
    assert files_of(v1, "*.jpg") == ["photo_a.jpg"]
    assert files_of(v1, "photo_?.*") == ["photo_a.jpg", "photo_b.png"]
    assert files_of(v1, "report") == ["annual_report_2024.pdf"]


def test_v1_extension_filters(v1):
    assert files_of(v1, "main ext:c") == ["main.c"]
    assert files_of(v1, "main ext:.cpp") == ["main.cpp"]
    assert files_of(v1, "photo ext:jpg,png") == ["photo_a.jpg", "photo_b.png"]
    assert files_of(v1, "main ext:!c") == ["main.cpp"]
    assert files_of(v1, "notes ext:txt") == ["draft_notes.txt"]


def test_v1_filter_only_and_ranges(v1):
    assert files_of(v1, "ext:png") == ["photo_b.png"]
    assert files_of(v1, "size:>2mb") == ["photo_a.jpg", "photo_b.png"]          # 2 MiB itself is not > 2mb
    assert files_of(v1, "size:>=2mb") == ["annual_report_2024.pdf", "photo_a.jpg", "photo_b.png"]
    assert files_of(v1, "photo size:3mb..3.5mb") == ["photo_a.jpg"]
    assert files_of(v1, "photo date:2020") == ["photo_a.jpg"]
    assert files_of(v1, "photo modified:2021-01-01..2022-12-31") == ["photo_b.png"]


def test_v1_path_filters(v1):
    assert files_of(v1, 'draft path:"my docs"') == ["final_draft.txt"]
    assert files_of(v1, "draft path:docs/my") == ["final_draft.txt"]
    assert files_of(v1, "draft path:docs\\my") == ["final_draft.txt"]          # either separator


def test_v1_case_regex_and_errors(v1):
    assert files_of(v1, "readme") == ["README.txt", "Readme.md"]
    assert files_of(v1, "cs:Readme") == ["Readme.md"]
    assert files_of(v1, "case:README") == ["README.txt"]
    assert files_of(v1, r"re:^main\.(c|cpp)$") == ["main.c", "main.cpp"]
    assert files_of(v1, "re:photo_[ab]") == ["photo_a.jpg", "photo_b.png"]
    r = v1.search(SearchRequest(1, "re:(["), NEVER, None)
    assert r.total == 0 and r.error and "regular expression" in r.error
    r = v1.search(SearchRequest(1, "size:>3parsecs"), NEVER, None)
    assert r.total == 0 and r.error


def test_v1_future_mtime_and_literal_wildcards(v1):
    assert files_of(v1, "future") == ["future.py"]
    assert files_of(v1, "50%_off") == ["50%_off_my_file.txt"]
    assert files_of(v1, "%") == ["50%_off_my_file.txt"]


def test_v1_limits_and_paging(v1):
    r = v1.search(SearchRequest(7, "txt", limit=0), NEVER, None)
    assert r.rows == () and r.total == 4
    r = v1.search(SearchRequest(8, "txt", limit=1), NEVER, None)
    p = v1.page(8, 1, 1)
    assert len(r.rows) == 1 and p.rows and p.rows[0].id != r.rows[0].id
    assert v1.page(8, 99, 10).rows == ()
    assert v1.page(7, 0, 10) is None            # not the latest search any more
    assert v1.page(8, -5, -1).rows == ()


def test_did_you_mean(v1):
    r = v1.search(SearchRequest(1, "photo_c"), NEVER, None)
    assert r.total == 0 and r.did_you_mean in ("photo_a", "photo_b")
    r = v1.search(SearchRequest(1, "raedme"), NEVER, None)
    assert r.did_you_mean == "readme"
    r = v1.search(SearchRequest(1, "anual"), NEVER, None)
    assert r.did_you_mean == "annual"
    assert v1.search(SearchRequest(1, "main"), NEVER, None).did_you_mean is None    # has hits
    assert v1.search(SearchRequest(1, "qqqqqqqq"), NEVER, None).did_you_mean is None


def test_cache_never_serves_stale_results_after_changes(tmp_path):
    store, m = tiny(tmp_path, ["a.txt"])
    rid = next(iter(m.root_slot))
    assert m.search(SearchRequest(1, "zebra"), NEVER, None).total == 0
    i = store.allocate_ids(1)[0]
    ops = [(INS, i, rid, "zebra.txt", 5, int(NOW), 0, 0)]
    m.apply(ops, store.apply(ops))
    assert m.search(SearchRequest(2, "zebra"), NEVER, None).total == 1
    ops = [(UPD, i, 999, int(NOW), 0, 0)]                # in-place size change is visible at once
    m.apply(ops, store.apply(ops))
    r = m.search(SearchRequest(3, "zebra size:>500"), NEVER, None)
    assert r.total == 1 and r.rows[0].size == 999
    ops = [(DEL, i)]
    m.apply(ops, store.apply(ops))
    assert m.search(SearchRequest(4, "zebra"), NEVER, None).total == 0
    store.close()


def test_folder_rename_and_delete_in_memory(tmp_path):
    store, m = tiny(tmp_path, [("inner.txt", "old", 1, 0)], folders=["old", "keep"])
    old = store.dir_lookup(ROOTS[0] + SEP + "old")[0]
    keep = store.dir_lookup(ROOTS[0] + SEP + "keep")[0]
    ops = [(REN, old, keep, "new"), (DIRPATH, old, ROOTS[0] + SEP + "keep" + SEP + "new")]
    m.apply(ops, store.apply(ops))
    r = m.search(SearchRequest(1, "inner"), NEVER, None)
    assert r.rows[0].path == SEP.join([ROOTS[0], "keep", "new", "inner.txt"]) and r.rows[0].rel_dir.endswith("new")
    assert m.search(SearchRequest(2, "keep/new/inner"), NEVER, None).total == 1
    assert m.search(SearchRequest(3, "inner", FilterState(path_contains="keep/new")), NEVER, None).total == 1
    ops = [(DEL, keep)]
    m.apply(ops, store.apply(ops))
    assert m.search(SearchRequest(4, "inner"), NEVER, None).total == 0
    assert m.search(SearchRequest(5, "keep"), NEVER, None).total == 0
    assert m.counters().total_entries == 0
    store.close()


# ------------------------------------------------------------------------------- suggestions

def test_name_suggestions_follow_the_filters(world):
    store, live, built, ref = world
    r = live.search(SearchRequest(1, "gift"), NEVER, None)
    names = [s for s in r.suggestions if s.kind == "name"]
    assert 1 <= len(names) <= 5
    assert len({s.text.lower() for s in names}) == len(names)
    assert all(s.count >= 1 for s in names)
    r = live.search(SearchRequest(2, "gift", FilterState(exts=frozenset({"pdf"}))), NEVER, None)
    assert all(s.text.lower().endswith(".pdf") for s in r.suggestions if s.kind == "name")
    r = live.search(SearchRequest(3, "index.js"), NEVER, None)
    assert all(s.text.lower() != "index.js" for s in r.suggestions)   # the typed text itself is skipped
    assert live.search(SearchRequest(4, r"re:index"), NEVER, None).suggestions == ()


def test_history_suggestions_are_verified_under_the_filters(world):
    store, live, built, ref = world
    hist = [HistoryItem("index", 3, True, 1.0), HistoryItem("index zzzqq", 0, False, 2.0),
            HistoryItem("indexing.md", 1, False, 3.0), HistoryItem("icon", 5, False, 4.0)]
    user = FakeUser(history=hist)
    r = live.search(SearchRequest(1, "ind"), NEVER, user)
    hs = [s for s in r.suggestions if s.kind == "history"]
    assert [s.text for s in hs][:1] == ["index"]                       # pinned first
    assert "index zzzqq" not in [s.text for s in hs]                    # no hits: never suggested
    assert all(s.count > 0 for s in hs)
    assert r.suggestions[0].kind == "history"
    # under a filter where "index" has no hits, it is not suggested
    r = live.search(SearchRequest(2, "ind", FilterState(exts=frozenset({"nope"}))), NEVER, user)
    assert [s for s in r.suggestions if s.kind == "history"] == []
    # empty text: recent history only (verified), no rows
    t0 = time.perf_counter()
    r = live.search(SearchRequest(3, ""), NEVER, user)
    assert (time.perf_counter() - t0) < 0.5
    assert r.total == 0 and r.rows == ()
    assert [s.text for s in r.suggestions] == ["index", "indexing.md", "icon"] or \
        set(s.text for s in r.suggestions) <= {"index", "indexing.md", "icon"}


# ------------------------------------------------------------------------------- cancellation

def test_cancelled_search_returns_none(world, monkeypatch):
    store, live, built, ref = world
    assert live.search(SearchRequest(1, "e"), lambda: True, None) is None
    monkeypatch.setattr(mi, "SEGMENT", 512)
    small = MemIndex.build(store)
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2          # turns true after the second segment
    assert small.search(SearchRequest(2, "e"), cancel, None) is None
    assert calls["n"] == 3


def test_search_and_apply_from_two_threads(tmp_path):
    store, m = tiny(tmp_path, [f"file_{i}.txt" for i in range(2000)])
    rid = next(iter(m.root_slot))
    errors = []

    def writer():
        try:
            for k in range(40):
                ids = store.allocate_ids(50)
                ops = [(INS, i, rid, f"w{k}_{i}.txt", 1, int(NOW), 0, 0) for i in ids]
                m.apply(ops, store.apply(ops))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    last = 0
    while t.is_alive():
        r = m.search(SearchRequest(1, "txt"), NEVER, None)
        assert r.total >= last
        last = r.total
    t.join()
    assert not errors
    assert m.search(SearchRequest(2, "txt"), NEVER, None).total == 2000 + 40 * 50
    store.close()


# ------------------------------------------------------------------------------- snapshot

QUERIES = ["e", "index", "gti", "-e", "*.js", "src/", "photo OR readme", "case:Index", r"re:\d"]


def results(m):
    return [[row for row in m.search(SearchRequest(1, q, limit=500), NEVER, None).rows] for q in QUERIES]


def test_snapshot_round_trip(world, tmp_path):
    store, live, built, ref = world
    p = tmp_path / snapshot.SNAPSHOT_NAME
    live.save_snapshot(p)
    loaded = MemIndex.load_snapshot(p, store.generation)
    assert loaded is not None
    assert loaded.generation == store.generation
    assert dump(loaded) == dump(live)
    assert results(loaded) == results(live)
    assert loaded.counters() == live.counters()
    # and it keeps working: apply + search
    rid = ref.root_ids[0]
    ops = [(INS, i, rid, "after_snapshot.txt", 1, int(NOW), 0, 0) for i in store.allocate_ids(1)]
    g = store.generation + 1000
    loaded.apply(ops, g)
    assert loaded.search(SearchRequest(1, "after_snapshot"), NEVER, None).total == 1


def test_snapshot_invalidation(world, tmp_path, monkeypatch):
    store, live, built, ref = world
    p = tmp_path / "s.snap"
    assert MemIndex.load_snapshot(p, 1) is None                      # missing
    live.save_snapshot(p)
    assert MemIndex.load_snapshot(p, store.generation + 1) is None   # stale generation
    assert not p.exists()                                            # ... and deleted
    live.save_snapshot(p)
    monkeypatch.setattr(snapshot, "FOLD_VERSION", 999)               # fold rules changed
    assert MemIndex.load_snapshot(p, store.generation) is None and not p.exists()
    monkeypatch.undo()
    monkeypatch.setattr(MemIndex, "clock", staticmethod(lambda: NOW))
    live.save_snapshot(p)
    real = snapshot.header_for
    monkeypatch.setattr(snapshot, "header_for", lambda g, n: real(g, n)[:3] + ((2, 7),) + real(g, n)[4:])
    assert MemIndex.load_snapshot(p, store.generation) is None       # other Python version
    monkeypatch.setattr(snapshot, "header_for", real)
    live.save_snapshot(p)
    data = p.read_bytes()
    p.write_bytes(data[: len(data) // 2])                            # truncated
    assert MemIndex.load_snapshot(p, store.generation) is None and not p.exists()
    p.write_bytes(b"\x00garbage" * 100)                               # not a snapshot
    assert MemIndex.load_snapshot(p, store.generation) is None and not p.exists()
    live.save_snapshot(p)
    assert MemIndex.load_snapshot(p, store.generation) is not None
    assert not list(tmp_path.glob("*.tmp*"))                          # temp files cleaned up


def test_snapshot_refreshes_age_buckets(world, tmp_path, monkeypatch):
    store, live, built, ref = world
    p = tmp_path / "s.snap"
    live.save_snapshot(p)
    monkeypatch.setattr(MemIndex, "clock", staticmethod(lambda: NOW + 400 * DAY))
    loaded = MemIndex.load_snapshot(p, store.generation)
    fresh = MemIndex.build(store)
    assert list(loaded.srank) == list(fresh.srank)
