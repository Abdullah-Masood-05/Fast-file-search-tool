#!/usr/bin/env python
"""Performance baseline / regression benchmark for Fast File Search Pro.

The script is binding-agnostic: it imports the app (which decides between PyQt6
and PySide6), detects which Qt binding the app actually loaded, and measures.

Run (from anywhere; put the app's ``src`` on PYTHONPATH, or run in an env where
the package is installed):

    set PYTHONPATH=<checkout>\\src
    python benchmarks\\bench_app.py                      # -> dist\\bench\\<binding>-<version>.json
    python benchmarks\\bench_app.py --out my.json --files 10000 --repeats 5
    python benchmarks\\bench_app.py --compare dist\\bench\\baseline-pyqt6.json dist\\bench\\pyside6-6.x.json
    python benchmarks\\bench_app.py --screenshots dist\\bench --tag pyside6   # light/dark PNGs

It always runs offscreen, with the cache/data dir and QSettings redirected to a
temp directory, so real user data is never touched.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from importlib import import_module
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

STEMS = ["report", "invoice", "photo", "résumé", "notes", "budget", "日本語", "archive", "draft", "readme"]
TOPICS = ["alpha", "beta", "gamma", "delta", "omega", "sigma", "kappa"]
EXTS = ["pdf", "txt", "docx", "jpg", "png", "py", "md", "csv", "xlsx", "mp3", "zip", "json"]
COLS = ["name", "folder", "size", "modified", "type"]


# ----------------------------------------------------------------------------- stats
def summarize(values):
    v = sorted(values)
    n = len(v)
    return {
        "median": statistics.median(v),
        "min": v[0],
        "p95": v[min(n - 1, max(0, math.ceil(0.95 * n) - 1))],
        "n": n,
    }


class Recorder:
    def __init__(self):
        self.metrics = {}

    def add(self, name, values, unit="ms", better="lower"):
        if not isinstance(values, (list, tuple)):
            values = [values]
        d = summarize(values)
        d["unit"] = unit
        d["better"] = better
        self.metrics[name] = d
        return d


def timed(fn, repeats, warmup=1):
    for _ in range(warmup):
        fn()
    out = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000.0)
    return out


def rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        try:
            import resource
            r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024
        except Exception:
            return None


# ----------------------------------------------------------------------------- Qt glue
class Qt:
    """Resolved after the app has been imported."""
    binding = version = qt_version = ""
    core = widgets = gui = None


def resolve_binding():
    import fast_file_search.ui.main_window as mw
    name = None
    for attr in ("QMainWindow", "QThread"):
        obj = getattr(mw, attr, None)
        mod = getattr(obj, "__module__", "") or ""
        if mod.split(".")[0] in ("PySide6", "PyQt6"):
            name = mod.split(".")[0]
            break
    if name is None:
        name = next((n for n in ("PySide6", "PyQt6") if n in sys.modules), None)
    if name is None:
        raise RuntimeError("could not detect the Qt binding used by the app")
    Qt.binding = name
    Qt.core = import_module(name + ".QtCore")
    Qt.widgets = import_module(name + ".QtWidgets")
    Qt.gui = import_module(name + ".QtGui")
    Qt.qt_version = Qt.core.qVersion()
    if name == "PySide6":
        Qt.version = import_module("PySide6").__version__
    else:
        Qt.version = Qt.core.PYQT_VERSION_STR
    Qt.Signal = getattr(Qt.core, "Signal", None) or getattr(Qt.core, "pyqtSignal")
    return mw


def isolate_env(tmp: Path):
    """Point every per-user location (config.json lives under %APPDATA%, not the
    cache dir) at tmp so a benchmark run can never read or write real user data."""
    for name, sub in (("FAST_FILE_SEARCH_CACHE_DIR", "cache"), ("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"),
                      ("XDG_CONFIG_HOME", "xdg_config"), ("XDG_DATA_HOME", "xdg_data"), ("XDG_CACHE_HOME", "xdg_cache")):
        d = tmp / sub
        d.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(d)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


def setup_isolation(tmp: Path):
    """Redirect QSettings (UI state) into tmp; call after the app is imported."""
    c = Qt.core
    settings_dir = tmp / "qsettings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    for scope in (c.QSettings.Scope.UserScope, c.QSettings.Scope.SystemScope):
        c.QSettings.setPath(c.QSettings.Format.IniFormat, scope, str(settings_dir))


def make_app():
    app = Qt.widgets.QApplication.instance() or Qt.widgets.QApplication(["bench"])
    app.setApplicationName("Fast File Search Pro")
    return app


def pump(app, cond=None, timeout=60.0, rounds=3):
    """Process events until cond() is true (or, without cond, a few rounds)."""
    if cond is None:
        for _ in range(rounds):
            app.processEvents()
        return True
    end = time.perf_counter() + timeout
    while not cond():
        app.processEvents()
        if time.perf_counter() > end:
            return False
    return True


def finish(data):
    sys.stdout.write(json.dumps(data) if not isinstance(data, str) else data)
    sys.stdout.flush()
    os._exit(0)  # skip interpreter/Qt teardown (threads, tray) - we measure, not shut down


# ----------------------------------------------------------------------------- (a) startup child
def child_startup():
    tmp = Path(os.environ["BENCH_TMP"])
    t0 = time.perf_counter()
    mw = resolve_binding()
    t1 = time.perf_counter()
    setup_isolation(tmp)
    app = make_app()
    t2 = time.perf_counter()
    win = mw.MainWindow()
    t3 = time.perf_counter()
    win.show()
    app.processEvents()
    t4 = time.perf_counter()
    app.processEvents()
    finish({
        "import_ms": (t1 - t0) * 1000, "qapp_ms": (t2 - t1) * 1000,
        "construct_ms": (t3 - t2) * 1000, "show_ms": (t4 - t3) * 1000,
        "construct_show_ms": (t4 - t2) * 1000, "total_ms": (t4 - t0) * 1000,
        "rss_mb": rss_mb(),
    })


def bench_startup(rec, tmp, runs=5):
    rows = []
    for i in range(runs + 1):  # first run warms OS file cache / .pyc, discarded
        d = tmp / f"startup_{i}"
        d.mkdir()
        env = dict(os.environ, BENCH_TMP=str(d))
        saved = dict(os.environ)
        isolate_env(d)
        env = dict(os.environ, BENCH_TMP=str(d))
        os.environ.clear(); os.environ.update(saved)
        t = time.perf_counter()
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child-startup"],
                           env=env, capture_output=True, text=True, timeout=300)
        wall = (time.perf_counter() - t) * 1000
        try:
            r = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            raise RuntimeError(f"startup child failed:\n{p.stdout}\n{p.stderr}")
        r["process_wall_ms"] = wall
        if i:
            rows.append(r)
    for key, label in (("import_ms", "import_main_window"), ("qapp_ms", "create_qapplication"),
                       ("construct_ms", "mainwindow_construct"), ("show_ms", "mainwindow_show"),
                       ("construct_show_ms", "mainwindow_construct_show"),
                       ("total_ms", "import_to_shown_total"), ("process_wall_ms", "process_wall_cold")):
        rec.add(f"startup.{label}", [r[key] for r in rows])
    if rows[0].get("rss_mb"):
        rec.add("memory.startup_child_rss", [r["rss_mb"] for r in rows], unit="MB")


# ----------------------------------------------------------------------------- data helpers
def make_results(n):
    from fast_file_search.search import SearchResult
    out = []
    for i in range(n):
        stem, topic, ext = STEMS[i % 10], TOPICS[(i // 3) % 7], EXTS[(i * 7) % 12]
        name = f"{stem}_{topic}_{i:06d}.{ext}"
        out.append(SearchResult(
            path=os.path.join(os.sep, "bench", f"dir_{i % 200:04d}", name), name=name, extension=ext,
            size=(i * 7919) % 5_000_000, modified=1_600_000_000 + (i * 977) % 90_000_000,
            is_folder=(i % 50 == 0), score=float((i * 31) % 997) / 10.0, matches=[stem]))
    return out


def make_response(n):
    from fast_file_search.search import SearchResponse
    return SearchResponse(results=make_results(n), total_count=n, page=1, page_size=n,
                          total_pages=1, query_time_ms=1.0)


def generate_tree(root: Path, total: int):
    """Deterministic tree of ~total entries (folders + files); returns the list of file paths."""
    per_dir = 74
    ndirs = max(1, total // (per_dir + 1))
    files = []
    for d in range(ndirs):
        label = "café" if d % 7 == 0 else ("データ" if d % 11 == 0 else "plain")
        folder = root / f"dir_{d:04d}_{label}"
        folder.mkdir(parents=True)
        for k in range(per_dir):
            i = d * per_dir + k
            stem, topic = STEMS[i % 10], TOPICS[(i // 3) % 7]
            ext = EXTS[(i * 7 + d) % 12]
            p = folder / f"{stem}_{topic}_{i:05d}.{ext}"
            p.write_bytes(b"x" * (i % 50 + 1))
            files.append(p)
    return files, ndirs


def db_size(path: Path):
    return sum(f.stat().st_size for f in path.parent.glob(path.name + "*") if f.is_file()) / (1024 * 1024)


# ----------------------------------------------------------------------------- (b)(c) index + search
def bench_index_and_search(rec, tmp, nfiles, repeats):
    from fast_file_search.config import AppConfig
    from fast_file_search.indexer import Indexer
    from fast_file_search.persistent_index import PersistentIndex
    from fast_file_search.search import SearchEngine

    root = tmp / "tree"
    t = time.perf_counter()
    files, ndirs = generate_tree(root, nfiles)
    print(f"  generated {len(files) + ndirs} entries in {time.perf_counter() - t:.1f}s", flush=True)
    entries = len(files) + ndirs
    cfg = AppConfig(index_paths=[str(root)])

    # Full scan: fresh DB each repeat.
    secs, last = [], None
    for r in range(max(2, repeats // 2 + 1)):
        dbp = tmp / f"full_{r}" / "index.db"
        idx = PersistentIndex(db_path=dbp)
        ix = Indexer(config=cfg, persistent_index=idx)
        t = time.perf_counter()
        stats = ix.index_path(str(root))
        secs.append(time.perf_counter() - t)
        seen = stats.total_files + stats.total_folders
        if r == 0 and abs(seen - entries) > entries * 0.02:
            print(f"  WARNING: indexer saw {seen} entries, expected {entries}", flush=True)
        if last is not None:
            last.close()
        last = idx
        last_path = dbp
    rec.add("index.full_scan", [s * 1000 for s in secs])
    rec.add("index.full_scan_rate", [entries / s for s in secs], unit="entries/s", better="higher")
    rec.add("index.db_size", db_size(last_path), unit="MB")

    # Incremental, no change, then 1% modified.
    ix = Indexer(config=cfg, persistent_index=last)
    nochange = []
    for _ in range(3):
        t = time.perf_counter()
        ix.incremental_update(str(root))
        nochange.append((time.perf_counter() - t) * 1000)
    rec.add("index.incremental_nochange", nochange)
    step = max(1, len(files) // max(1, len(files) // 100))
    inc = []
    for r in range(3):
        chosen = files[r::step][: max(1, len(files) // 100)]
        for p in chosen:
            p.write_bytes(b"y" * (60 + r))
            st = p.stat()
            os.utime(p, (st.st_atime, st.st_mtime + 10 + r))
        t = time.perf_counter()
        stats = ix.incremental_update(str(root))
        inc.append((time.perf_counter() - t) * 1000)
        if r == 0:
            print(f"  incremental: updated={stats.updated} added={stats.added} "
                  f"unchanged={stats.unchanged} of {len(chosen)} touched", flush=True)
    rec.add("index.incremental_1pct", inc)
    rec.add("index.incremental_1pct_rate", [entries / (x / 1000) for x in inc], unit="entries/s", better="higher")

    # Search
    engine = SearchEngine(persistent_index=last)
    queries = {
        "substring_report": "report",
        "substring_invoice_alpha": "invoice_alpha",
        "substring_unicode": "résumé",
        "wildcard": "pho*gamma_000??",
        "regex": r"re:^invoice_(alpha|beta)_\d{5}\.pdf$",
        "ext_filter": "report ext:pdf",
        "multi_term_and": "report alpha 001",
    }
    counts = {}

    def run(q):
        def f():
            engine.invalidate_cache()
            resp = engine.search_persistent(q, 1, 1000)
            counts[q] = resp.total_count
        return f

    for name, q in queries.items():
        rec.add(f"search.{name}", timed(run(q), max(7, repeats * 2), warmup=2))
        rec.metrics[f"search.{name}"]["result_count"] = counts[q]
    last.close()


# ----------------------------------------------------------------------------- (d)(e)(f) UI
def bench_ui(rec, tmp, app, win, repeats):
    c = Qt.core
    sv = win.search_view
    # MainWindow keeps its own filter panel; fall back to a standalone one.
    fp = getattr(win, "filter_panel", None)
    if fp is None:
        from fast_file_search.ui.widgets import FilterPanel
        fp = FilterPanel()
    win.resize(1280, 800)
    pump(app)
    rec.add("memory.rss_after_startup", rss_mb() or 0, unit="MB")
    terms = ["report"]
    responses = {n: make_response(n) for n in (1000, 10000)}

    def load(n):
        t = time.perf_counter()
        sv.set_results(responses[n], terms)
        t_call = time.perf_counter() - t
        pump(app)
        t_pump = time.perf_counter() - t
        sv.results_view.grab()  # paint the visible rows
        t_paint = time.perf_counter() - t
        return t_call * 1000, t_pump * 1000, t_paint * 1000

    cold = load(10000)
    rec.add("ui.results_10000_first_load_cold", cold[2])
    rec.add("memory.rss_after_10000_results", rss_mb() or 0, unit="MB")
    sv.set_results(make_response(0), terms)
    pump(app)

    for n in (1000, 10000):
        rows = []
        reps = repeats if n == 1000 else max(3, repeats - 1)
        load(n)  # warm
        for _ in range(reps):
            sv.set_results(make_response(0), terms)
            pump(app)
            rows.append(load(n))
        rec.add(f"ui.results_{n}.set_results_call", [r[0] for r in rows])
        rec.add(f"ui.results_{n}.until_events_processed", [r[1] for r in rows])
        rec.add(f"ui.results_{n}.until_first_paint", [r[2] for r in rows])

    # Sorting (10k rows loaded)
    sv.set_results(responses[10000], terms)
    pump(app)
    asc, desc = c.Qt.SortOrder.AscendingOrder, c.Qt.SortOrder.DescendingOrder
    ncols = min(len(COLS), sv.model.columnCount())
    for col in range(ncols):
        for order, tag in ((asc, "asc"), (desc, "desc")):
            def f(col=col, order=order):
                sv.model.sort(col, order)
                pump(app)
                sv.results_view.grab()
            rec.add(f"ui.sort_10000.{COLS[col]}_{tag}", timed(f, repeats))
    if hasattr(sv, "sort_by_relevance"):
        rec.add("ui.sort_10000.relevance", timed(lambda: (sv.sort_by_relevance(), pump(app)), repeats))

    # Favorites filter toggling over 10k rows
    def fav():
        sv.toggle_fav_filter(True)
        pump(app)
        sv.toggle_fav_filter(False)
        pump(app)
    rec.add("ui.favorites_filter_toggle_10000", timed(fav, repeats))

    # Filter panel changes
    fp.update_extensions(sorted(set(EXTS) | {f"ext{i}" for i in range(40)}))
    pump(app)

    def filter_cycle():
        for k in (1, 3, 5):
            if fp.ext_combo.count() > k:
                fp.ext_combo.setCurrentIndex(k)
            if fp.size_combo.count() > k % fp.size_combo.count():
                fp.size_combo.setCurrentIndex(k % fp.size_combo.count())
            if fp.date_combo.count() > 1:
                fp.date_combo.setCurrentIndex(k % fp.date_combo.count())
            fp.regex_input.setText(r"dir_\d+")
            fp.exclude_input.setText("*.tmp, node_modules")
            fp.get_filter_values()
            pump(app, rounds=1)
        fp.reset_filters()
        fp.get_filter_values()
        pump(app)
    rec.add("ui.filter_panel_change_cycle", timed(filter_cycle, repeats))

    # (e) Preview
    pv = getattr(win, "preview_panel", None)
    if pv is not None:
        line = "The quick brown fox jumps over the lazy dog - lorem ipsum report 0123456789\n"
        for label, size in (("100KB", 100 * 1024), ("2MB", 2 * 1024 * 1024)):
            p = tmp / f"preview_{label}.txt"
            p.write_text(line * (size // len(line)), encoding="utf-8")
            pv.set_search_terms(["lorem", "report"])

            def text_shown():
                return bool(pv.text_preview.toPlainText())

            def load_prev():
                pv.clear()
                pump(app)
                t = time.perf_counter()
                pv.load_file(str(p))
                ok = pump(app, text_shown, timeout=30)
                t1 = (time.perf_counter() - t) * 1000
                th = getattr(pv, "loader_thread", None)
                if th is not None:
                    pump(app, lambda: th.isFinished(), timeout=30)
                t2 = (time.perf_counter() - t) * 1000
                if not ok:
                    raise RuntimeError("preview never loaded")
                return t1, t2
            load_prev()
            rows = [load_prev() for _ in range(repeats)]
            rec.add(f"preview.{label}.text_visible", [r[0] for r in rows])
            rec.add(f"preview.{label}.loader_finished", [r[1] for r in rows])


# ----------------------------------------------------------------------------- (g) signals / threads
def bench_signals(rec, app, repeats):
    c = Qt.core
    Signal = Qt.Signal
    N_SIG, N_TIMER = 20000, 10000

    class Receiver(c.QObject):
        def __init__(self):
            super().__init__()
            self.count = 0

        def on_value(self, _v):
            self.count += 1

    class Emitter(c.QThread):
        obj_sig = Signal(object)
        int_sig = Signal(int)

        def __init__(self, n, kind):
            super().__init__()
            self.n, self.kind = n, kind

        def run(self):
            if self.kind == "int":
                emit = self.int_sig.emit
            else:
                emit = self.obj_sig.emit
            payload = {"path": "x", "n": 1}
            for i in range(self.n):
                emit(i if self.kind == "int" else payload)

    class Local(c.QObject):
        sig = Signal(object)

    def cross(kind):
        def f():
            rcv = Receiver()
            th = Emitter(N_SIG, kind)
            (th.int_sig if kind == "int" else th.obj_sig).connect(rcv.on_value)
            t = time.perf_counter()
            th.start()
            if not pump(app, lambda: rcv.count >= N_SIG, timeout=120):
                raise RuntimeError(f"only {rcv.count} signals arrived")
            dt = time.perf_counter() - t
            th.wait()
            return dt
        return f

    for kind in ("object", "int"):
        f = cross(kind)
        f()
        secs = [f() for _ in range(repeats)]
        rec.add(f"signals.cross_thread_{kind}_{N_SIG}", [s * 1000 for s in secs])
        rec.add(f"signals.cross_thread_{kind}_throughput", [N_SIG / s for s in secs],
                unit="signals/s", better="higher")

    def timers():
        cnt = [0]

        def cb():
            cnt[0] += 1
        t = time.perf_counter()
        for _ in range(N_TIMER):
            c.QTimer.singleShot(0, cb)
        pump(app, lambda: cnt[0] >= N_TIMER, timeout=120)
        return (time.perf_counter() - t) * 1000
    timers()
    rec.add(f"signals.singleshot0_{N_TIMER}", [timers() for _ in range(repeats)])

    def same_thread():
        loc, rcv = Local(), Receiver()
        loc.sig.connect(rcv.on_value)
        t = time.perf_counter()
        for i in range(100000):
            loc.sig.emit(i)
        return (time.perf_counter() - t) * 1000
    same_thread()
    rec.add("signals.same_thread_emit_100000", [same_thread() for _ in range(repeats)])


# ----------------------------------------------------------------------------- screenshots
def screenshots(outdir: Path, tag: str):
    tmp = Path(tempfile.mkdtemp(prefix="ffs_shot_"))
    isolate_env(tmp)
    try:
        mw = resolve_binding()
        setup_isolation(tmp)
        app = make_app()
        from fast_file_search.search import SearchResponse, SearchResult
        from fast_file_search.ui import theme
        # Real files so icons and the preview panel have something to show.
        docs = tmp / "docs"
        docs.mkdir()
        results = []
        for i in range(60):
            stem, topic, ext = STEMS[i % 10], TOPICS[i % 7], ["txt", "md", "py", "csv", "json", "pdf"][i % 6]
            p = docs / f"{stem}_{topic}_{i:03d}.{ext}"
            p.write_text(f"Quarterly report {i}\nlorem ipsum report notes\n" * 40, encoding="utf-8")
            st = p.stat()
            results.append(SearchResult(path=str(p), name=p.name, extension=ext, size=st.st_size,
                                        modified=int(st.st_mtime) - i * 86400 * 3, is_folder=False,
                                        score=100.0 - i, matches=["report"]))
        resp = SearchResponse(results=results, total_count=len(results), page=1, page_size=100,
                              total_pages=1, query_time_ms=3.4)
        win = mw.MainWindow()
        win.resize(1280, 800)
        win.show()
        for mode in ("light", "dark"):
            if hasattr(win, "set_appearance"):
                win.set_appearance(mode)
            else:
                theme.apply_appearance(mode)
            sv = win.search_view
            sv.search_input.blockSignals(True)
            sv.search_input.setText("report")
            sv.search_input.blockSignals(False)
            sv.set_results(resp, ["report"])
            if hasattr(sv, "select_path"):
                sv.select_path(results[0].path)
            if hasattr(win, "preview_panel"):
                win.preview_panel.set_search_terms(["report"])
                win.preview_panel.clear()
                win.preview_panel.load_file(results[0].path)
            end = time.perf_counter() + 0.8
            while time.perf_counter() < end:
                app.processEvents()
                time.sleep(0.005)
            outdir.mkdir(parents=True, exist_ok=True)
            target = outdir / f"{tag}_{mode}.png"
            ok = win.grab().save(str(target))
            print(f"screenshot {target} saved={ok}")
        finish("")
    finally:
        pass


# ----------------------------------------------------------------------------- compare
def flatten(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d, d.get("metrics", {})


ABS_FLOOR = {"ms": 5.0, "MB": 5.0}


def compare(a_path, b_path):
    red = "\033[31m" if (sys.stdout.isatty() or os.environ.get("FORCE_COLOR")) else ""
    rst = "\033[0m" if red else ""
    ma, A = flatten(a_path)
    mb, B = flatten(b_path)
    la = f"{ma['meta']['binding']} {ma['meta']['version']}"
    lb = f"{mb['meta']['binding']} {mb['meta']['version']}"
    print(f"A = {la} ({a_path})\nB = {lb} ({b_path})\n")
    w = max(len(k) for k in set(A) | set(B))
    print(f"{'metric':<{w}}  {'unit':>10}  {'A median':>12}  {'B median':>12}  {'change':>8}  flag")
    regressions = 0
    for k in sorted(set(A) | set(B), key=lambda x: (x.split('.')[0], x)):
        a, b = A.get(k), B.get(k)
        if not a or not b:
            print(f"{k:<{w}}  {'':>10}  {'n/a' if not a else a['median']:>12}  {'n/a' if not b else b['median']:>12}")
            continue
        va, vb, unit = a["median"], b["median"], a.get("unit", "")
        pct = ((vb - va) / va * 100.0) if va else 0.0
        worse = (vb - va) if a.get("better", "lower") == "lower" else (va - vb)
        worse_pct = (worse / va * 100.0) if va else 0.0
        bad = worse_pct > 10.0 and abs(vb - va) > ABS_FLOOR.get(unit, 0.0)
        flag = "REGRESSION" if bad else ("improved" if worse_pct < -10.0 and abs(vb - va) > ABS_FLOOR.get(unit, 0.0) else "")
        regressions += bad
        line = f"{k:<{w}}  {unit:>10}  {va:>12.2f}  {vb:>12.2f}  {pct:>+7.1f}%  {flag}"
        print(f"{red}{line}{rst}" if bad else line)
    print(f"\n{regressions} regression(s) (>10% worse and >5 ms / >5 MB absolute where applicable)")
    return 1 if regressions else 0


# ----------------------------------------------------------------------------- main
def print_table(rec, meta):
    print(f"\n=== {meta['binding']} {meta['version']} (Qt {meta['qt_version']}), Python {meta['python']} ===")
    w = max(len(k) for k in rec.metrics)
    print(f"{'metric':<{w}}  {'unit':>10}  {'median':>12}  {'min':>12}  {'p95':>12}  n")
    for k, d in rec.metrics.items():
        print(f"{k:<{w}}  {d['unit']:>10}  {d['median']:>12.2f}  {d['min']:>12.2f}  {d['p95']:>12.2f}  {d['n']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="JSON output path (default dist/bench/<binding>-<version>.json)")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"))
    ap.add_argument("--files", type=int, default=30000, help="synthetic tree size (files+folders)")
    ap.add_argument("--repeats", type=int, default=7)
    ap.add_argument("--startup-runs", type=int, default=5)
    ap.add_argument("--skip", default="", help="comma list of sections to skip: startup,ui,signals,index")
    ap.add_argument("--screenshots", metavar="DIR", help="save populated light/dark window PNGs and exit")
    ap.add_argument("--tag", default="shot", help="file prefix for --screenshots")
    ap.add_argument("--child-startup", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.compare:
        sys.exit(compare(*args.compare))
    if args.child_startup:
        child_startup()
    if args.screenshots:
        screenshots(Path(args.screenshots), args.tag)

    skip = set(filter(None, args.skip.split(",")))
    tmp = Path(tempfile.mkdtemp(prefix="ffs_bench_"))
    isolate_env(tmp)
    rec = Recorder()
    try:
        mw = resolve_binding()
        meta = {
            "binding": Qt.binding, "version": Qt.version, "qt_version": Qt.qt_version,
            "python": platform.python_version(), "platform": platform.platform(),
            "date": time.strftime("%Y-%m-%d %H:%M:%S"), "repeats": args.repeats,
            "files": args.files,
        }
        print(f"Binding: {Qt.binding} {Qt.version} (Qt {Qt.qt_version}), Python {meta['python']}", flush=True)
        if "startup" not in skip:
            print("[a] startup (subprocess x%d)" % args.startup_runs, flush=True)
            bench_startup(rec, tmp, args.startup_runs)
        setup_isolation(tmp)
        app = make_app()
        if "ui" not in skip:
            print("[d-f] UI, preview, memory", flush=True)
            win = mw.MainWindow()
            win.show()
            bench_ui(rec, tmp, app, win, args.repeats)
        if "signals" not in skip:
            print("[g] signals / threads", flush=True)
            bench_signals(rec, app, args.repeats)
        if "index" not in skip:
            print("[b-c] index build + search", flush=True)
            bench_index_and_search(rec, tmp, args.files, args.repeats)
        out = Path(args.out) if args.out else REPO / "dist" / "bench" / f"{Qt.binding.lower()}-{Qt.version}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"meta": meta, "metrics": rec.metrics}, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print_table(rec, meta)
        print(f"\nwrote {out}", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    os._exit(0)


if __name__ == "__main__":
    main()
