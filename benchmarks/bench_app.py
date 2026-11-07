#!/usr/bin/env python
"""End-to-end benchmark of the 2.0 app: the real ``main.main()`` with the real engine process.

    uv run python benchmarks/bench_app.py                       # 30k-file synthetic tree -> dist/bench/app-<platform>.json
    uv run python benchmarks/bench_app.py --files 300000 --out bench-app.json
    uv run python benchmarks/bench_app.py --tree D:\\some\\tree    # use an existing tree (not modified)
    uv run python benchmarks/bench_app.py --compare A.json B.json   # side by side, flags regressions (>10% and >5 ms/MB)
    uv run python benchmarks/bench_app.py --screenshots dist\\bench --tag v2  # light/dark PNGs of the real window
    uv run python benchmarks/bench_app.py --profile-gui dist\\prof  # GUI-thread profile while typing (gui_profile.py)

Measured (see ``metrics`` in the JSON; ``headline`` repeats the numbers CI checks):
  startup (fresh process, warm index):  to first paint, to engine Ready, to first results
  fresh index:                          entries/s from Ready to idle (cold index, 16 ms heartbeat + typing)
  heartbeat:                            GUI-thread lateness while indexing + typing, and while typing on the idle index
  search through the client:            round trip per query class (1/2/3-char, word, miss) and overall p95
  keystroke -> results:                 last keystroke to the results being on screen (includes the 120 ms debounce)
  watcher burst:                        N files created under a live root until a search sees all of them
  memory:                               RSS of the GUI process and of the engine process

Everything per-user (config, index cache, Qt settings, logs) is redirected into a temp sandbox, so a run never
touches real data. The GUI runs on whatever Qt platform is available (offscreen unless QT_QPA_PLATFORM is set).
The benchmark child processes re-run this file; the spawned engine process re-imports it harmlessly (all work
is under ``__main__``).
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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
QUERIES = {
    "1char": "e", "2char": "in", "3char": "gif", "word": "readme", "rare": "gifttop",
    "unicode": "日本", "miss": "zzzqq",
}
TYPING_WORDS = ["gifttop", "index", "readme", "lodash", "userprofile", "zzzqq", "button", "invoice"]
SANDBOX_VARS = (("HOME", "home"), ("USERPROFILE", "home"), ("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"),
                ("XDG_CONFIG_HOME", "xdg-config"), ("XDG_CACHE_HOME", "xdg-cache"), ("XDG_DATA_HOME", "xdg-data"),
                ("XDG_STATE_HOME", "xdg-state"), ("FAST_FILE_SEARCH_CACHE_DIR", "cache"))


# ----------------------------------------------------------------------------- shared helpers
def sandbox_env(sandbox: Path, env: dict | None = None) -> dict:
    """Redirect every per-user location into ``sandbox`` (in ``env`` or os.environ)."""
    target = os.environ if env is None else env
    for name, sub in SANDBOX_VARS:
        d = sandbox / sub
        d.mkdir(parents=True, exist_ok=True)
        target[name] = str(d)
    target.setdefault("QT_QPA_PLATFORM", "offscreen")
    if sys.platform == "win32":
        target.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")   # the offscreen plugin has no fonts otherwise
    target["FFS_POWER"] = "ac"
    return target


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(p / 100 * len(xs)) - 1))]


def rss_mb(pid: int | None = None):
    """Resident set size of ``pid`` (default: this process) in MB, or None."""
    pid = pid or os.getpid()
    try:
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
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000 | 0x0010, False, pid)    # QUERY_LIMITED_INFORMATION | VM_READ
            if not h:
                return None
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ok = psapi.GetProcessMemoryInfo(wintypes.HANDLE(h), ctypes.byref(pmc), pmc.cb)
            k32.CloseHandle(wintypes.HANDLE(h))
            return pmc.WorkingSetSize / 2 ** 20 if ok else None
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip()) / 1024
    except Exception:
        return None


def summary(late):
    return {"n": len(late), "p50": round(pct(late, 50) or 0, 2), "p99": round(pct(late, 99) or 0, 2),
            "max": round(max(late), 2) if late else None,
            "over_30ms": sum(1 for x in late if x > 30), "over_50ms": sum(1 for x in late if x > 50)}


# ----------------------------------------------------------------------------- child: drives the real app
def _child_common(sandbox: Path, tree: Path | None):
    sandbox_env(sandbox)
    sys.path.insert(0, str(REPO / "src"))
    if tree is not None:
        from fast_file_search.config import get_config_manager
        cm = get_config_manager()
        cm.config.index_paths = [str(tree)]
        cm.save()


class _Heartbeat:
    def __init__(self, qt, interval=16):
        QTimer, QElapsedTimer, Qt = qt
        self.interval = interval
        self.t = QTimer()
        self.t.setTimerType(Qt.TimerType.PreciseTimer)
        self.t.setInterval(interval)
        self.t.timeout.connect(self._tick)
        self.clock = QElapsedTimer()
        self.last = 0
        self.last_pc = 0.0           # time.perf_counter() of the last tick (read by the stall sampler)
        self.late = []

    def start(self):
        self.late = []
        self.clock.start()
        self.last = self.clock.nsecsElapsed()
        self.last_pc = time.perf_counter()
        self.t.start()

    def _tick(self):
        now = self.clock.nsecsElapsed()
        self.late.append(max(0.0, (now - self.last) / 1e6 - self.interval))
        self.last = now
        self.last_pc = time.perf_counter()

    def take(self):
        late, self.late = self.late, []
        return late


def _run_app(driver_factory, out_path: Path, out: dict, probe=None) -> int:
    """main.main([]) with MainWindow replaced by a subclass that starts the driver.
    ``probe`` (profile runs only) gets a mark for every request sent and every result applied."""
    import fast_file_search.main as app_main
    import fast_file_search.ui.main_window as mw_mod
    from PySide6.QtCore import QTimer

    Orig = mw_mod.MainWindow

    class Window(Orig):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._t_applied = None
            self._applied_total = None
            QTimer.singleShot(0, lambda: driver_factory(self))

        def _on_search_result(self, res):
            t = time.perf_counter()
            super()._on_search_result(res)
            if res.offset == 0 and self._displayed_key == self._req_key:
                self._t_applied = time.perf_counter()
            if probe is not None:
                probe.apply(res.gen, res.offset, res.refresh, (time.perf_counter() - t) * 1000)

        def _send_search(self, commit=False, text=None):
            t = time.perf_counter()
            super()._send_search(commit, text)
            if probe is not None:
                probe.send(self._req_raw, self._gen, (time.perf_counter() - t) * 1000)

    mw_mod.MainWindow = Window
    try:
        out["rc"] = app_main.main([])
    finally:
        mw_mod.MainWindow = Orig
        out_path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


def _is_idle(win):
    st = win._status
    return (st is not None and st.state == "idle" and bool(st.roots)
            and all(r.state == "ok" and r.last_scan_end is not None for r in st.roots))


def child_startup(sandbox: Path, out_path: Path) -> int:
    """One launch against the already-built index: first paint, Ready, first results."""
    t0 = float(os.environ["BENCH_T0"])
    _child_common(sandbox, None)
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtTest import QTest

    out: dict = {}
    marks: dict = {}

    class PaintWatcher(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Type.Paint and "paint" not in marks:
                marks["paint"] = time.time()
            return False

    def driver(win):
        watcher = PaintWatcher(win)
        win._paint_watcher = watcher
        win.installEventFilter(watcher)
        win.central_widget.installEventFilter(watcher)
        client = win.client
        client.ready.connect(lambda r: marks.setdefault("ready", time.time()))
        client.search_result.connect(lambda r: marks.setdefault("results", time.time()) if r.total else None)
        client.ready.connect(lambda r: client.search("gif", suggestions=False))
        end = time.monotonic() + 60
        while time.monotonic() < end and "results" not in marks:
            QTest.qWait(5)
        QTest.qWait(500)
        out["first_paint_ms"] = (marks.get("paint", float("nan")) - t0) * 1000
        out["ready_ms"] = (marks.get("ready", float("nan")) - t0) * 1000
        out["first_results_ms"] = (marks.get("results", float("nan")) - t0) * 1000
        out["spawn_to_ready_ms"] = getattr(client, "spawn_to_ready_ms", None)
        st = client.last_status
        out["entries"] = st.total_entries if st else None
        out["rss_gui_mb"] = rss_mb()
        out["rss_engine_mb"] = rss_mb(client.engine_pid)
        win.close_and_quit()

    return _run_app(driver, out_path, out)


def child_run(sandbox: Path, tree: Path, out_path: Path, burst_files: int) -> int:
    """Fresh index with typing, search latency, keystroke latency, watcher burst, RSS."""
    _child_common(sandbox, tree)
    from PySide6.QtCore import QElapsedTimer, QEvent, QEventLoop, Qt, QTimer
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    out: dict = {"tree": str(tree)}
    prof_dir = os.environ.get("BENCH_PROFILE_DIR")
    probe = gp = None
    if prof_dir:
        sys.path.insert(0, str(HERE))
        import gui_profile as gp
        from fast_file_search.engine import protocol as P
        probe = gp.Probe()
        probe.install(P)

    def key(f, ch):
        if ch.isascii():
            QTest.keyClicks(f, ch)
            return
        for t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(f, QKeyEvent(t, 0, Qt.KeyboardModifier.NoModifier, ch))

    def driver(win):
        try:
            run(win)
        except Exception:
            import traceback
            out["error"] = traceback.format_exc()
            traceback.print_exc()
        finally:
            win.close_and_quit()

    def run(win):
        client = win.client
        hb = _Heartbeat((QTimer, QElapsedTimer, Qt))
        hb.start()
        sampler = None
        if probe is not None:
            import threading
            probe.phase = "indexing_typing"
            sampler = gp.StallSampler(threading.get_ident(), hb, probe)
            sampler.start()
        win.activateWindow()
        f = win.bar.field
        # ---- fresh index while typing
        end = time.monotonic() + 120
        while not client.is_ready and time.monotonic() < end:
            QTest.qWait(5)
        t_ready = time.perf_counter()
        out["spawn_to_ready_ms"] = getattr(client, "spawn_to_ready_ms", None)
        keys, w, key_ms = 0, 0, []
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline and not _is_idle(win):
            word = TYPING_WORDS[w % len(TYPING_WORDS)]
            w += 1
            f.setFocus()
            f.clear()
            for ch in word:
                t = time.perf_counter()
                key(f, ch)
                key_ms.append((time.perf_counter() - t) * 1000)
                keys += 1
                QTest.qWait(90)
            QTest.qWait(150)
        t_idle = time.perf_counter()
        st = win._status
        out["index"] = {"entries": st.total_entries, "folders": st.total_folders,
                        "ready_to_idle_s": round(t_idle - t_ready, 3),
                        "entries_per_s": round(st.total_entries / max(1e-3, t_idle - t_ready)),
                        "keys_typed": keys}
        out["heartbeat_indexing_typing"] = summary(hb.take())
        out["key_handling_ms"] = {"p50": round(pct(key_ms, 50) or 0, 2), "p99": round(pct(key_ms, 99) or 0, 2)}
        out["rss_after_index_gui_mb"] = rss_mb()
        out["rss_after_index_engine_mb"] = rss_mb(client.engine_pid)
        print("index:", out["index"], flush=True)
        QTest.qWait(1000)

        # ---- search latency through the client (no UI debounce)
        got = {}

        loop = QEventLoop()

        def on_res(res):
            if not res.refresh and res.offset == 0:
                got.setdefault(res.gen, (time.perf_counter(), res))
                loop.quit()

        def wait_for_gen(gen, timeout):
            """Block in the Qt event loop (not a spin loop: a spinning main thread starves the
            client's reader thread of the GIL and would inflate the numbers)."""
            end = time.monotonic() + timeout
            while gen not in got and time.monotonic() < end:
                QTimer.singleShot(int(max(1, (end - time.monotonic()) * 1000)), loop.quit)
                loop.exec()
        client.search_result.connect(on_res)
        lat = {name: [] for name in QUERIES}
        engine_ms = {name: [] for name in QUERIES}
        totals = {}
        if probe is not None:
            probe.phase = "search"
        reps = int(os.environ.get("BENCH_SEARCH_REPS", "5" if probe is not None else "15"))
        for rep in range(reps + 2):
            for name, text in QUERIES.items():
                t = time.perf_counter()
                gen = client.search(text, suggestions=False, limit=200)
                wait_for_gen(gen, 30)
                if gen not in got:
                    continue
                t2, res = got.pop(gen)
                totals[name] = res.total
                if rep >= 2:                      # two warm-up rounds
                    lat[name].append((t2 - t) * 1000)
                    engine_ms[name].append(res.elapsed_ms)
                QTest.qWait(5)
        client.search_result.disconnect(on_res)
        allv = [x for v in lat.values() for x in v]
        out["search_client_ms"] = {
            n: {"p50": round(pct(v, 50), 2), "p95": round(pct(v, 95), 2), "max": round(max(v), 2),
                "engine_p50": round(pct(engine_ms[n], 50), 2), "total": totals.get(n)}
            for n, v in lat.items() if v}
        out["search_client_all"] = {"p50": round(pct(allv, 50), 2), "p95": round(pct(allv, 95), 2),
                                   "max": round(max(allv), 2), "n": len(allv)}
        print("search through the client:", out["search_client_all"], flush=True)

        # ---- keystroke -> results on screen, heartbeat running
        def ev_wait(ms):
            """Wait in a real event loop (what the app runs), not QTest.qWait's processEvents + 10 ms sleeps."""
            loop = QEventLoop()
            QTimer.singleShot(ms, loop.quit)
            loop.exec()

        def typing_pass(reps, drop_first, wait=QTest.qWait):
            kr, words, over = {}, [], []

            def pause(ms):
                t = time.perf_counter()
                wait(ms)
                if ms >= 90:
                    over.append((time.perf_counter() - t) * 1000 - ms)
            hb.take()
            for rep in range(reps):
                for word in TYPING_WORDS:
                    f.setFocus()
                    f.clear()
                    pause(150)
                    win._t_applied = None
                    t_first = time.perf_counter()
                    t_last = 0.0
                    for ch in word:
                        key(f, ch)
                        t_last = time.perf_counter()
                        pause(90)
                    e = time.monotonic() + 10
                    while (win._req_raw != word or win._t_applied is None
                           or win._displayed_key != win._req_key) and time.monotonic() < e:
                        pause(5)
                    if win._t_applied is not None and (rep or not drop_first):   # drop the first round
                        kr.setdefault(word, []).append((win._t_applied - t_last) * 1000)
                        words.append((word, t_first, t_last, time.perf_counter()))
                    pause(100)
            allk = [x for v in kr.values() for x in v]
            res = {"keystroke_to_results_ms": {"p50": round(pct(allk, 50), 1), "p95": round(pct(allk, 95), 1),
                                               "max": round(max(allk), 1), "debounce_ms": 120, "n": len(allk)},
                   "heartbeat": summary(hb.take())}
            return res, words, over

        if probe is None:
            res, _w, _o = typing_pass(4, True)
            out["keystroke_to_results_ms"] = res["keystroke_to_results_ms"]
            out["heartbeat_typing_idle"] = res["heartbeat"]
        else:
            out["profile"] = profile_typing(typing_pass, ev_wait, probe, sampler, gp, Path(prof_dir))
            first = out["profile"]["passes"]["qwait"]
            out["keystroke_to_results_ms"] = first["keystroke_to_results_ms"]
            out["heartbeat_typing_idle"] = first["heartbeat"]
        print("keystroke -> results:", out["keystroke_to_results_ms"], flush=True)
        if probe is not None:
            return                      # profile runs stop here (no watcher burst)

        # ---- watcher burst under the live root
        burst_dir = tree / "zz_bench_burst"
        burst_dir.mkdir(exist_ok=True)
        token = "zzburstfile"
        gen_box = {}

        def on_burst(res):
            if not res.refresh:
                gen_box[res.gen] = res.total
        client.search_result.connect(on_burst)
        t_burst = time.perf_counter()
        for i in range(burst_files):
            (burst_dir / f"{token}{i}.dat").write_bytes(b"b")
        t_written = time.perf_counter()
        seen_at = None
        end = time.monotonic() + 120
        while time.monotonic() < end:
            g = client.search(token, suggestions=False, limit=1)
            e2 = time.monotonic() + 5
            while g not in gen_box and time.monotonic() < e2:
                QTest.qWait(5)
            if gen_box.get(g) == burst_files:
                seen_at = time.perf_counter()
                break
            QTest.qWait(50)
        client.search_result.disconnect(on_burst)
        out["watcher_burst"] = {"files": burst_files, "write_s": round(t_written - t_burst, 2),
                                "searchable_after_write_s": round(seen_at - t_written, 2) if seen_at else None,
                                "ok": seen_at is not None}
        print("watcher burst:", out["watcher_burst"], flush=True)
        shutil.rmtree(burst_dir, ignore_errors=True)

        out["rss_final_gui_mb"] = rss_mb()
        out["rss_final_engine_mb"] = rss_mb(client.engine_pid)
        out["engine_pid"] = client.engine_pid

    return _run_app(driver, out_path, out, probe)


def profile_typing(typing_pass, ev_wait, probe, sampler, gp, prof_dir: Path) -> dict:
    """The typing-on-an-idle-index scenario four times: QTest.qWait waits (the normal benchmark),
    real event-loop waits, under cProfile, and (macOS) under /usr/bin/sample."""
    import cProfile
    from PySide6 import __version__ as pyside
    from PySide6.QtCore import qVersion
    from PySide6.QtGui import QGuiApplication

    prof = {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
            "qt": qVersion(), "pyside": pyside, "qpa": QGuiApplication.platformName(), "passes": {}}

    def run_pass(name, reps, drop_first, wait=None):
        probe.phase = name
        i0 = len(probe.events)
        t0 = time.perf_counter()
        res, words, over = typing_pass(reps, drop_first, wait) if wait else typing_pass(reps, drop_first)
        res["seconds"] = round(time.perf_counter() - t0, 1)
        res["wait_overshoot_ms"] = gp.stats(over)
        res["phases"] = gp.keystroke_phases(probe.events[i0:], words)
        prof["passes"][name] = res
        print(f"profile pass {name}: {res['keystroke_to_results_ms']} heartbeat {res['heartbeat']}", flush=True)
        return res

    first = run_pass("qwait", 3, True)
    run_pass("evloop", 2, False, ev_wait)
    sampler.paused = True
    pr = cProfile.Profile()
    pr.enable()
    try:
        run_pass("cprofile", 2, False)
    finally:
        pr.disable()
    sampler.paused = False
    prof["cprofile"] = gp.cprofile_top(pr)
    prof_dir.mkdir(parents=True, exist_ok=True)
    pr.dump_stats(str(prof_dir / "gui-cprofile.pstats"))
    if sys.platform == "darwin":
        secs = first["seconds"] * 2 / 3 + 4
        path = str(prof_dir / "native-sample.txt")
        proc = gp.start_native_sample(secs, path)
        if proc is None:
            prof["native"] = "(could not start /usr/bin/sample)"
        else:
            run_pass("native", 2, False)
            try:
                proc.wait(timeout=secs + 60)
            except subprocess.TimeoutExpired:
                proc.kill()
            prof["native"] = gp.native_main_thread_summary(path)
    sampler.stop()
    prof["stalls"] = sampler.report()
    (prof_dir / "gui-profile.json").write_text(json.dumps(prof, indent=1, ensure_ascii=False), encoding="utf-8")
    (prof_dir / "gui-profile.txt").write_text(gp.text_report(prof), encoding="utf-8")
    return {"passes": prof["passes"]}


def child_shots(sandbox: Path, outdir: Path, tag: str) -> int:
    """Light and dark PNGs of the real window on a small real tree."""
    docs = sandbox / "shots-tree"
    docs.mkdir(parents=True, exist_ok=True)
    stems = ["report", "invoice", "photo", "notes", "budget", "archive", "draft", "readme"]
    for i in range(80):
        ext = ["txt", "md", "py", "csv", "json", "pdf"][i % 6]
        p = docs / f"{stems[i % 8]}_{i:03d}.{ext}"
        p.write_text(f"Quarterly report {i}\nlorem ipsum report notes\n" * 30, encoding="utf-8")
    _child_common(sandbox, docs)
    from PySide6.QtTest import QTest

    def driver(win):
        try:
            end = time.monotonic() + 60
            while not _is_idle(win) and time.monotonic() < end:
                QTest.qWait(20)
            win.resize(1280, 800)
            f = win.bar.field
            f.setFocus()
            QTest.keyClicks(f, "report")
            QTest.qWait(700)
            win.results.select_row(0)
            outdir.mkdir(parents=True, exist_ok=True)
            for mode in ("light", "dark"):
                win.set_appearance(mode)
                QTest.qWait(500)
                target = outdir / f"{tag}_{mode}.png"
                print(f"screenshot {target} saved={win.grab().save(str(target))}", flush=True)
        finally:
            win.close_and_quit()

    return _run_app(driver, sandbox / "shots.json", {})


# ----------------------------------------------------------------------------- parent: orchestration
def summarize(values):
    v = sorted(values)
    return {"median": statistics.median(v), "min": v[0], "p95": pct(v, 95), "n": len(v)}


class Recorder:
    def __init__(self):
        self.metrics = {}

    def add(self, name, values, unit="ms", better="lower"):
        if values is None:
            return
        values = [x for x in (values if isinstance(values, (list, tuple)) else [values])
                  if x is not None and not (isinstance(x, float) and math.isnan(x))]
        if not values:
            return
        d = summarize(values)
        d.update(unit=unit, better=better)
        self.metrics[name] = d


def run_child(mode_args, sandbox: Path, timeout: int):
    env = sandbox_env(sandbox, dict(os.environ))
    env["BENCH_T0"] = repr(time.time())
    cmd = [sys.executable, str(Path(__file__).resolve()), *mode_args]
    p = subprocess.run(cmd, env=env, timeout=timeout, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    sys.stdout.write(p.stdout)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-4000:])
        raise RuntimeError(f"benchmark child {mode_args[0]} failed with {p.returncode}")
    return p


def make_tree(target: Path, files: int):
    sys.path.insert(0, str(HERE))
    from gen_tree import generate
    t = time.perf_counter()
    n, d = generate(str(target), files, max(10, files // 15), seed=7)
    print(f"generated {n + d} entries ({n} files) in {time.perf_counter() - t:.1f}s", flush=True)
    return n + d


def qt_versions():
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        return PySide6.__version__, qVersion()
    except Exception:
        return "unknown", "unknown"


def benchmark(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ffs_bench_"))
    sandbox = tmp / "sandbox"
    sandbox.mkdir()
    try:
        if args.tree:
            tree = Path(args.tree).resolve()
        else:
            tree = tmp / "tree"
            make_tree(tree, args.files)
        rec = Recorder()
        child_json = tmp / "run.json"
        print("[1/2] fresh index, typing, search, watcher burst", flush=True)
        run_child(["--child-run", str(sandbox), str(tree), str(child_json), str(args.burst_files)], sandbox,
                  timeout=3600)
        run = json.loads(child_json.read_text(encoding="utf-8"))
        if "error" in run:
            raise RuntimeError("benchmark driver failed:\n" + run["error"])

        starts = []
        print(f"[2/2] startup against the built index (x{args.startup_runs})", flush=True)
        for i in range(args.startup_runs + 1):           # the first launch warms the OS cache, discarded
            sj = tmp / f"startup_{i}.json"
            run_child(["--child-startup", str(sandbox), str(sj)], sandbox, timeout=600)
            row = json.loads(sj.read_text(encoding="utf-8"))
            if i:
                starts.append(row)

        idx = run["index"]
        rec.add("startup.first_paint", [r["first_paint_ms"] for r in starts])
        rec.add("startup.engine_ready", [r["ready_ms"] for r in starts])
        rec.add("startup.first_results", [r["first_results_ms"] for r in starts])
        rec.add("index.fresh_rate", idx["entries_per_s"], unit="entries/s", better="higher")
        rec.add("index.fresh_ready_to_idle", idx["ready_to_idle_s"] * 1000)
        rec.add("index.spawn_to_ready_cold", run.get("spawn_to_ready_ms"))
        rec.add("heartbeat.indexing_typing_p99", run["heartbeat_indexing_typing"]["p99"])
        rec.add("heartbeat.indexing_typing_max", run["heartbeat_indexing_typing"]["max"])
        rec.add("heartbeat.typing_idle_p99", run["heartbeat_typing_idle"]["p99"])
        rec.add("heartbeat.typing_idle_max", run["heartbeat_typing_idle"]["max"])
        rec.add("search.client_p95", run["search_client_all"]["p95"])
        rec.add("search.client_p50", run["search_client_all"]["p50"])
        for name, d in run["search_client_ms"].items():
            rec.add(f"search.client.{name}", d["p50"])
        rec.add("keystroke.to_results_p50", run["keystroke_to_results_ms"]["p50"])
        rec.add("keystroke.to_results_p95", run["keystroke_to_results_ms"]["p95"])
        rec.add("keystroke.handling_p99", run["key_handling_ms"]["p99"])
        wb = run["watcher_burst"]
        rec.add("watcher.burst_searchable_after_write", (wb["searchable_after_write_s"] or float("nan")) * 1000)
        rec.add("memory.gui_rss_after_index", run["rss_after_index_gui_mb"], unit="MB")
        rec.add("memory.engine_rss_after_index", run["rss_after_index_engine_mb"], unit="MB")
        rec.add("memory.gui_rss_final", run["rss_final_gui_mb"], unit="MB")
        rec.add("memory.engine_rss_final", run["rss_final_engine_mb"], unit="MB")
        rec.add("memory.gui_rss_startup", [r["rss_gui_mb"] for r in starts], unit="MB")
        rec.add("memory.engine_rss_startup", [r["rss_engine_mb"] for r in starts], unit="MB")

        binding, qt = qt_versions()
        import sqlite3
        meta = {"binding": "PySide6", "version": binding, "qt_version": qt, "python": platform.python_version(),
                "platform": platform.platform(), "machine": platform.machine(), "sqlite": sqlite3.sqlite_version,
                "date": time.strftime("%Y-%m-%d %H:%M:%S"), "files": args.files, "entries": idx["entries"],
                "startup_runs": args.startup_runs, "cpu_count": os.cpu_count()}
        headline = {
            "entries": idx["entries"],
            "index_entries_per_s": idx["entries_per_s"],
            "search_p95_ms": run["search_client_all"]["p95"],
            "heartbeat_p99_ms": max(run["heartbeat_indexing_typing"]["p99"], run["heartbeat_typing_idle"]["p99"]),
            "heartbeat_max_ms": max(run["heartbeat_indexing_typing"]["max"] or 0, run["heartbeat_typing_idle"]["max"] or 0),
            "startup_first_paint_ms": rec.metrics["startup.first_paint"]["median"],
            "startup_ready_ms": rec.metrics["startup.engine_ready"]["median"],
            "keystroke_to_results_p95_ms": run["keystroke_to_results_ms"]["p95"],
            "watcher_burst_ok": wb["ok"],
            "gui_rss_mb": run["rss_final_gui_mb"],
            "engine_rss_mb": run["rss_final_engine_mb"],
        }
        result = {"meta": meta, "headline": headline, "metrics": rec.metrics, "raw": {"run": run, "startup": starts}}
        out = Path(args.out) if args.out else REPO / "dist" / "bench" / f"app-{sys.platform}-{idx['entries']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print_table(rec, meta)
        print("\nheadline:", json.dumps(headline))
        print(f"wrote {out}", flush=True)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def profile_gui(args) -> int:
    """Fresh index + the typing scenarios with the GUI-thread probes (see gui_profile.py). Writes
    gui-profile.json/.txt, gui-cprofile.pstats, run.json and (macOS) native-sample.txt into the folder."""
    prof_dir = Path(args.profile_gui).resolve()
    prof_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ffs_prof_"))
    sandbox = tmp / "sandbox"
    sandbox.mkdir()
    try:
        if args.tree:
            tree = Path(args.tree).resolve()
        else:
            tree = tmp / "tree"
            make_tree(tree, args.files)
        os.environ["BENCH_PROFILE_DIR"] = str(prof_dir)
        try:
            run_child(["--child-run", str(sandbox), str(tree), str(prof_dir / "run.json"), "0"], sandbox,
                      timeout=3600)
        finally:
            os.environ.pop("BENCH_PROFILE_DIR", None)
        run = json.loads((prof_dir / "run.json").read_text(encoding="utf-8"))
        if "error" in run:
            raise RuntimeError("benchmark driver failed:\n" + run["error"])
        txt = prof_dir / "gui-profile.txt"
        print(txt.read_text(encoding="utf-8") if txt.exists() else "(no gui-profile.txt)", flush=True)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def print_table(rec, meta):
    print(f"\n=== Fast File Search Pro 2.0 app benchmark ({meta['platform']}, Python {meta['python']}, "
          f"{meta['binding']} / Qt {meta['qt_version']}, {meta['entries']:,} entries) ===")
    w = max(len(k) for k in rec.metrics)
    print(f"{'metric':<{w}}  {'unit':>10}  {'median':>12}  {'min':>12}  {'p95':>12}  n")
    for k, d in rec.metrics.items():
        print(f"{k:<{w}}  {d['unit']:>10}  {d['median']:>12.2f}  {d['min']:>12.2f}  {d['p95']:>12.2f}  {d['n']}")


# ----------------------------------------------------------------------------- compare
ABS_FLOOR = {"ms": 5.0, "MB": 5.0}


def compare(a_path, b_path) -> int:
    red = "\033[31m" if (sys.stdout.isatty() or os.environ.get("FORCE_COLOR")) else ""
    rst = "\033[0m" if red else ""
    A = json.loads(Path(a_path).read_text(encoding="utf-8"))
    B = json.loads(Path(b_path).read_text(encoding="utf-8"))

    def label(d, p):
        m = d.get("meta", {})
        return f"{m.get('platform', '?')} {m.get('version', '')} ({p})"
    print(f"A = {label(A, a_path)}\nB = {label(B, b_path)}\n")
    MA, MB = A.get("metrics", {}), B.get("metrics", {})
    w = max(len(k) for k in set(MA) | set(MB))
    print(f"{'metric':<{w}}  {'unit':>10}  {'A median':>12}  {'B median':>12}  {'change':>8}  flag")
    regressions = 0
    for k in sorted(set(MA) | set(MB), key=lambda x: (x.split('.')[0], x)):
        a, b = MA.get(k), MB.get(k)
        if not a or not b:
            print(f"{k:<{w}}  {'':>10}  {'n/a' if not a else a['median']:>12}  {'n/a' if not b else b['median']:>12}")
            continue
        va, vb, unit = a["median"], b["median"], a.get("unit", "")
        change = ((vb - va) / va * 100.0) if va else 0.0
        worse = (vb - va) if a.get("better", "lower") == "lower" else (va - vb)
        worse_pct = (worse / va * 100.0) if va else 0.0
        floor = ABS_FLOOR.get(unit, 0.0)
        bad = worse_pct > 10.0 and abs(vb - va) > floor
        flag = "REGRESSION" if bad else ("improved" if worse_pct < -10.0 and abs(vb - va) > floor else "")
        regressions += bad
        line = f"{k:<{w}}  {unit:>10}  {va:>12.2f}  {vb:>12.2f}  {change:>+7.1f}%  {flag}"
        print(f"{red}{line}{rst}" if bad else line)
    print(f"\n{regressions} regression(s) (>10% worse and >5 ms / >5 MB absolute where applicable)")
    return 1 if regressions else 0


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="JSON output path (default dist/bench/app-<platform>-<entries>.json)")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"))
    ap.add_argument("--files", type=int, default=30_000, help="synthetic tree size in files (folders are extra)")
    ap.add_argument("--tree", help="benchmark an existing tree instead of generating one (not modified)")
    ap.add_argument("--startup-runs", type=int, default=3)
    ap.add_argument("--burst-files", type=int, default=2000)
    ap.add_argument("--screenshots", metavar="DIR", help="save light/dark PNGs of the real window and exit")
    ap.add_argument("--tag", default="v2", help="file prefix for --screenshots")
    ap.add_argument("--profile-gui", metavar="DIR",
                    help="profile the GUI thread while typing (cProfile, stall stacks, per-keystroke phases, "
                         "macOS native samples) and write the reports into DIR; no startup runs or burst")
    # internal: the app runs in child processes
    ap.add_argument("--child-run", nargs=4, metavar=("SANDBOX", "TREE", "OUT", "BURST"), help=argparse.SUPPRESS)
    ap.add_argument("--child-startup", nargs=2, metavar=("SANDBOX", "OUT"), help=argparse.SUPPRESS)
    ap.add_argument("--child-shots", nargs=3, metavar=("SANDBOX", "DIR", "TAG"), help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.compare:
        return compare(*args.compare)
    if args.child_run:
        s, t, o, b = args.child_run
        return child_run(Path(s), Path(t), Path(o), int(b))
    if args.child_startup:
        return child_startup(Path(args.child_startup[0]), Path(args.child_startup[1]))
    if args.child_shots:
        return child_shots(Path(args.child_shots[0]), Path(args.child_shots[1]), args.child_shots[2])
    if args.profile_gui:
        return profile_gui(args)
    if args.screenshots:
        tmp = Path(tempfile.mkdtemp(prefix="ffs_shot_"))
        try:
            run_child(["--child-shots", str(tmp / "sandbox"), str(Path(args.screenshots).resolve()), args.tag],
                      tmp / "sandbox", timeout=300)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0
    return benchmark(args)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
