"""P2 acceptance benchmark (design H.2). Not collected by pytest.

    uv run --python 3.13 --with watchdog python tests/engine/bench_service.py [tree] [--no-builtin]

Every app path is sandboxed into a temp dir. Uses the real engine through
multiprocessing spawn (engine_main) except where engine internals are measured
(in-process Engine over a real Pipe).
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="ffs-p2-bench-"))
for _v in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
           "FAST_FILE_SEARCH_CACHE_DIR"):
    os.environ[_v] = str(SANDBOX / _v.lower())
    os.makedirs(os.environ[_v], exist_ok=True)
os.environ["HOME"] = os.environ["USERPROFILE"] = str(SANDBOX / "home")
os.makedirs(os.environ["HOME"], exist_ok=True)
os.environ["FFS_POWER"] = "ac"
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import multiprocessing as mp  # noqa: E402

from fast_file_search.engine import protocol as P  # noqa: E402
from fast_file_search.engine.service import engine_main  # noqa: E402

TREE = next((a for a in sys.argv[1:] if not a.startswith("--")),
            os.path.join(os.environ.get("TEMP", "/tmp"), "ffs_diag_tree"))
if "--no-builtin" in sys.argv:
    os.environ["FFS_NO_BUILTIN_EXCLUDES"] = "1"
RESULTS = {}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))]


class Spawned:
    def __init__(self, app: Path, roots, **cfg):
        self.boot = P.EngineBootstrap(P.EngineConfig(roots=tuple(roots), **cfg), str(app / "cache"),
                                      str(app / "data"), str(app / "logs"))
        self.msgs = []
        self.cv = threading.Condition()
        ctx = mp.get_context("spawn")
        self.a, b = ctx.Pipe()
        self.t0 = time.perf_counter()
        self.p = ctx.Process(target=engine_main, args=(b, self.boot), daemon=True)
        self.p.start()
        b.close()
        self.rx = threading.Thread(target=self._rx, daemon=True)
        self.rx.start()

    def _rx(self):
        try:
            while True:
                m = self.a.recv()
                with self.cv:
                    self.msgs.append((time.perf_counter(), m))
                    self.cv.notify_all()
        except (EOFError, OSError):
            pass

    def wait(self, pred, timeout=120, start=0):
        end = time.monotonic() + timeout
        with self.cv:
            while True:
                for i in range(start, len(self.msgs)):
                    t, m = self.msgs[i]
                    if pred(m):
                        return t, m
                if time.monotonic() > end:
                    raise TimeoutError([type(m).__name__ for _t, m in self.msgs[-10:]])
                self.cv.wait(0.1)

    def mark(self):
        with self.cv:
            return len(self.msgs)

    def send(self, m):
        self.a.send(m)

    def search(self, gen, text, timeout=30):
        mark = self.mark()
        t = time.perf_counter()
        self.send(P.SearchRequest(gen, text))
        t2, r = self.wait(lambda m: isinstance(m, P.SearchResult) and m.gen == gen and not m.refresh, timeout, mark)
        return (t2 - t) * 1000, r

    def close(self):
        try:
            self.send(P.Shutdown())
        except OSError:
            pass
        self.p.join(10)
        if self.p.is_alive():
            self.p.kill()


def idle(m):
    return isinstance(m, P.IndexStatus) and m.state == "idle"


def bench_index_and_startup():
    app = SANDBOX / "app1"
    # fresh index end to end (spawn -> idle)
    e = Spawned(app, [TREE], live_monitoring=False)
    t_ready, ready = e.wait(lambda m: isinstance(m, P.Ready))
    RESULTS["spawn_to_ready_empty_ms"] = (t_ready - e.t0) * 1000
    t_idle, st = e.wait(lambda m: idle(m) and m.total_entries > 0, timeout=600)
    n = st.total_entries
    dt = t_idle - e.t0
    RESULTS["fresh_index_entries"] = n
    RESULTS["fresh_index_s"] = dt
    RESULTS["fresh_index_rate"] = n / dt
    RESULTS["ready_capabilities"] = ready.capabilities
    print(f"fresh index: {n:,} entries in {dt:.2f}s = {n / dt:,.0f}/s  (spawn->ready {RESULTS['spawn_to_ready_empty_ms']:.0f} ms)")
    # idle RTT
    rtts = []
    for i, q in enumerate(["index", "gifttopicon", "readme", "button", "component", "userprofile", "lodash", "main",
                           "report", "zzq"] * 3):
        ms, _ = e.search(1000 + i, q)
        rtts.append(ms)
    RESULTS["idle_rtt_p50_ms"], RESULTS["idle_rtt_p95_ms"] = pct(rtts, 50), pct(rtts, 95)
    print(f"idle search RTT p50 {pct(rtts, 50):.1f} p95 {pct(rtts, 95):.1f} ms")
    # incremental no-change rescan
    mark = e.mark()
    t = time.perf_counter()
    e.send(P.IndexCommand("rescan"))
    e.wait(lambda m: isinstance(m, P.IndexStatus) and m.state == "scanning", 30, mark)
    t2, _ = e.wait(idle, 120, e.mark())
    RESULTS["rescan_nochange_s"] = t2 - t
    print(f"incremental no-change rescan: {t2 - t:.2f}s")
    # 200 touched files reflected after a rescan
    files = []
    for dp, dn, fn in os.walk(TREE):
        for f in fn:
            files.append(os.path.join(dp, f))
            if len(files) >= 200:
                break
        if len(files) >= 200:
            break
    future = time.time() + 3600
    for f in files:
        os.utime(f, (future, future))
    sample = files[::20]                     # 10 of the 200, spread over the walk
    mark = e.mark()
    t = time.perf_counter()
    e.send(P.IndexCommand("rescan"))
    pending = set(sample)
    gen = 5000
    t_reflected = None
    while pending and time.perf_counter() - t < 30:
        for f in list(pending):
            gen += 1
            _ms, r = e.search(gen, os.path.basename(f))
            if any(row.path == f and row.mtime == int(future) for row in r.rows):
                pending.discard(f)
        if not pending:
            t_reflected = time.perf_counter() - t
            break
        time.sleep(0.05)
    t2, _ = e.wait(idle, 120, mark)
    RESULTS["rescan_200_touched_reflected_s"] = t_reflected
    RESULTS["rescan_200_touched_full_rescan_s"] = t2 - t
    print(f"200 touched files reflected {t_reflected:.2f}s after the rescan started "
          f"(whole rescan {t2 - t:.2f}s; sampled 10/200)" if t_reflected else "touched files NOT reflected")
    e.close()

    # restart: with snapshot (written at clean shutdown)
    e = Spawned(app, [TREE], live_monitoring=False)
    t_ready, ready = e.wait(lambda m: isinstance(m, P.Ready))
    RESULTS["spawn_to_ready_snapshot_ms"] = (t_ready - e.t0) * 1000
    print(f"spawn->Ready with snapshot: {RESULTS['spawn_to_ready_snapshot_ms']:.0f} ms ({ready.capabilities})")
    e.close()
    # restart: no snapshot -> rebuild from SQLite
    try:
        os.remove(app / "cache" / "index-v5.snap")
    except OSError:
        pass
    e = Spawned(app, [TREE], live_monitoring=False)
    t_ready, ready = e.wait(lambda m: isinstance(m, P.Ready))
    RESULTS["spawn_to_ready_sqlite_ms"] = (t_ready - e.t0) * 1000
    print(f"spawn->Ready from SQLite (no snapshot): {RESULTS['spawn_to_ready_sqlite_ms']:.0f} ms "
          f"(snapshot used: {'snapshot' in ready.capabilities})")
    e.close()


def bench_rtt_while_indexing():
    app = SANDBOX / "app2"
    e = Spawned(app, [TREE], live_monitoring=False)
    e.wait(lambda m: isinstance(m, P.Ready))
    qs = ["index", "gifttopicon", "readme", "button", "component", "userprofile", "lodash", "main", "report"]
    rtts = []
    gen = 0
    t_start = time.perf_counter()
    while True:
        gen += 1
        ms, r = e.search(gen, qs[gen % len(qs)])
        rtts.append(ms)
        time.sleep(max(0.0, 0.09 - ms / 1000))
        st = [m for _t, m in e.msgs if isinstance(m, P.IndexStatus)]
        if st and st[-1].state == "idle" and st[-1].total_entries > 0:
            break
        if time.perf_counter() - t_start > 300:
            break
    scan_s = time.perf_counter() - t_start
    during = rtts[:-1]
    idle_rtts = []
    for i in range(30):
        ms, _ = e.search(10000 + i, qs[i % len(qs)])
        idle_rtts.append(ms)
        time.sleep(0.09)
    RESULTS["indexing_rtt_p95_ms"] = pct(during, 95)
    RESULTS["indexing_rtt_p50_ms"] = pct(during, 50)
    RESULTS["indexing_rtt_n"] = len(during)
    RESULTS["indexing_scan_s_with_typing"] = scan_s
    RESULTS["idle2_rtt_p95_ms"] = pct(idle_rtts, 95)
    print(f"RTT while indexing (search every 90 ms, n={len(during)}): p50 {pct(during, 50):.1f} p95 "
          f"{pct(during, 95):.1f} ms; idle p95 {pct(idle_rtts, 95):.1f} ms; ratio "
          f"{pct(during, 95) / pct(idle_rtts, 95):.2f}; scan took {scan_s:.1f}s while typing")
    e.close()


def bench_inprocess_overhead_and_watcher():
    """In-process Engine over a real Pipe: per-search send overhead and watcher costs."""
    from fast_file_search.engine.service import Engine
    app = SANDBOX / "app3"
    root = SANDBOX / "watchroot"
    (root / "proj" / "node_modules").mkdir(parents=True)
    (root / "burst").mkdir()
    boot = P.EngineBootstrap(P.EngineConfig(roots=(TREE, str(root))), str(app / "cache"), str(app / "data"),
                             str(app / "logs"))
    a, b = mp.Pipe()
    got = []
    cv = threading.Condition()

    def rx():
        try:
            while True:
                m = a.recv()
                with cv:
                    got.append(m)
                    cv.notify_all()
        except (EOFError, OSError):
            pass
    threading.Thread(target=rx, daemon=True).start()
    eng = Engine(boot, b.send)
    eng.start()

    def wait(pred, timeout=300):
        end = time.monotonic() + timeout
        with cv:
            while True:
                for m in got:
                    if pred(m):
                        return m
                if time.monotonic() > end:
                    raise TimeoutError
                cv.wait(0.1)
    wait(lambda m: isinstance(m, P.Ready))
    wait(lambda m: idle(m) and m.total_entries > 0, 600)
    time.sleep(1)
    for i, q in enumerate(["index", "readme", "e", "main", "gifttopicon", "component", "in", "report"] * 4):
        eng.handle(P.SearchRequest(100 + i, q))
        wait(lambda m, g=100 + i: isinstance(m, P.SearchResult) and m.gen == g)
    t = [x for x in eng.search.timings if x[2] == 200]
    sends = [x[1] for x in t]
    RESULTS["send_overhead_200rows_p50_ms"] = pct(sends, 50)
    RESULTS["send_overhead_200rows_max_ms"] = max(sends)
    RESULTS["memindex_search_p50_ms"] = pct([x[0] for x in t], 50)
    print(f"engine overhead after MemIndex.search (pickle + pipe, 200 rows): p50 {pct(sends, 50):.2f} ms, "
          f"max {max(sends):.2f} ms (search p50 {pct([x[0] for x in t], 50):.1f} ms)")

    # 5000-create burst -> searchable
    script = ("import os,sys\nd=sys.argv[1]\nfor i in range(int(sys.argv[2])):\n"
              "    open(os.path.join(d, f'burstfile{i}.dat'),'w').close()\n")
    subprocess.run([sys.executable, "-c", script, str(root / "burst"), "5000"], check=True)
    t_end = time.perf_counter()
    gen = 500
    while True:
        gen += 1
        eng.handle(P.SearchRequest(gen, "burstfile"))
        r = wait(lambda m, g=gen: isinstance(m, P.SearchResult) and m.gen == g)
        if r.total >= 5000 or time.perf_counter() - t_end > 15:
            break
        time.sleep(0.05)
    lat = time.perf_counter() - t_end
    RESULTS["burst5000_searchable_s"] = lat
    RESULTS["burst5000_total"] = r.total
    print(f"5,000-create burst fully searchable {lat:.2f}s after the burst (total {r.total})")

    # 20,000 events in node_modules: engine CPU and ops
    time.sleep(1.5)
    ops0, b0 = eng.stats.ops, eng.stats.batches
    script = ("import os,sys\nd=sys.argv[1]\nfor i in range(10000):\n    p=os.path.join(d,f'f{i}.js')\n"
              "    open(p,'w').close()\n    open(p,'a').write('x')\n")
    cpu0 = time.process_time()
    subprocess.run([sys.executable, "-c", script, str(root / "proj" / "node_modules")], check=True)
    time.sleep(3.0)
    cpu = time.process_time() - cpu0
    seen = eng.watcher.events_seen if eng.watcher else 0
    RESULTS["nm_flood_engine_cpu_s"] = cpu
    RESULTS["nm_flood_ops"] = eng.stats.ops - ops0
    RESULTS["nm_flood_events_seen"] = seen
    print(f"20,000 node_modules events: engine CPU {cpu:.3f}s (incl. 3 s idle), ops {eng.stats.ops - ops0}, "
          f"batches {eng.stats.batches - b0}, watcher events seen so far {seen}")
    eng.stop()
    a.close()


if __name__ == "__main__":
    print(sys.version.split()[0], "tree", TREE, "builtin excludes", os.environ.get("FFS_NO_BUILTIN_EXCLUDES") != "1")
    which = [a for a in sys.argv[1:] if a.startswith("--only=")]
    only = which[0].split("=", 1)[1].split(",") if which else ["index", "rtt", "inproc"]
    if "index" in only:
        bench_index_and_startup()
    if "rtt" in only:
        bench_rtt_while_indexing()
    if "inproc" in only:
        bench_inprocess_overhead_and_watcher()
    print(json.dumps(RESULTS, indent=1, default=str))
