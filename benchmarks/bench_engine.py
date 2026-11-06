#!/usr/bin/env python
"""Engine acceptance benchmark (design H.1 and H.2 matrices) in one JSON.

    uv run python benchmarks/bench_engine.py --tree <dir> --scale 1 --out bench-engine.json
    uv run python benchmarks/bench_engine.py --files 300000 --scale 3          # generates its own tree
    uv run python benchmarks/bench_engine.py --tree <dir> --walker-threads 1,2,4,8   # sweep (macOS D4 default)

It runs the existing acceptance scripts as subprocesses and merges their output:
  * ``tests/engine/bench_local.py``   (P1: store/memindex apply, build, snapshot, RSS, latency matrix, suggestions,
                                       cancellation, exactness).  ``--scale 3`` replicates the index to ~1M entries.
  * ``tests/engine/bench_service.py`` (P2: spawn-to-ready, fresh index rate through the real engine process,
                                       search round trips while indexing, in-process overhead, watcher bursts).
  * ``--walker-threads N,M,...``       one fresh-index run of bench_service per value, with FFS_WALKER_THREADS set.
Each script sandboxes every app path into its own temp dir; nothing outside it is written.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ENGINE_TESTS = REPO / "tests" / "engine"


def run(cmd, env=None, timeout=7200):
    t = time.perf_counter()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    dt = time.perf_counter() - t
    if p.returncode != 0:
        sys.stderr.write(p.stdout[-3000:] + "\n" + p.stderr[-3000:] + "\n")
        raise RuntimeError(f"{' '.join(map(str, cmd[:3]))} failed with {p.returncode}")
    return p.stdout, dt


def trailing_json(text: str) -> dict:
    """The scripts print progress lines and finish with one indented JSON object."""
    start = text.rfind("\n{\n")
    start = 0 if text.startswith("{\n") and start < 0 else start + 1
    return json.loads(text[start:])


def tail(text: str, n: int = 25) -> None:
    for line in text.strip().splitlines()[-n:]:
        print("   ", line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tree", help="tree to index (default: generate --files files in a temp dir)")
    ap.add_argument("--files", type=int, default=300_000, help="files to generate when --tree is not given")
    ap.add_argument("--scale", type=int, default=1, choices=[1, 3], help="1 = the tree as is, 3 = replicate to ~1M entries")
    ap.add_argument("--runs", type=int, default=5, help="repeats per latency case")
    ap.add_argument("--no-ref", action="store_true", help="skip the exactness check against the reference evaluator")
    ap.add_argument("--only", default="local,service", help="comma list of: local, service")
    ap.add_argument("--walker-threads", default="", help="e.g. 1,2,4,8: fresh-index rate per FFS_WALKER_THREADS value")
    ap.add_argument("--out", default="", help="JSON output path (default dist/bench/engine-<platform>-s<scale>.json)")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    tmp = Path(tempfile.mkdtemp(prefix="ffs_bench_engine_"))
    try:
        if args.tree:
            tree = Path(args.tree).resolve()
        else:
            tree = tmp / "tree"
            print(f"generating {args.files} files ...", flush=True)
            run([sys.executable, str(HERE / "gen_tree.py"), str(tree), "--files", str(args.files)])
        result = {"meta": {"python": platform.python_version(), "platform": platform.platform(),
                           "machine": platform.machine(), "sqlite": sqlite3.sqlite_version,
                           "cpu_count": os.cpu_count(), "scale": args.scale, "tree": str(tree),
                           "date": time.strftime("%Y-%m-%d %H:%M:%S")}}
        py = sys.executable
        env = dict(os.environ, PYTHONUTF8="1")

        if "local" in only:
            print(f"[local] bench_local.py --scale {args.scale}", flush=True)
            local_json = tmp / "local.json"
            cmd = [py, str(ENGINE_TESTS / "bench_local.py"), "--tree", str(tree), "--scale", str(args.scale),
                   "--runs", str(args.runs), "--out", str(local_json)]
            if args.no_ref:
                cmd.append("--no-ref")
            stdout, dt = run(cmd, env)
            tail(stdout, 6)
            result["local"] = json.loads(local_json.read_text(encoding="utf-8"))
            result["local_wall_s"] = round(dt, 1)

        if "service" in only:
            print("[service] bench_service.py", flush=True)
            stdout, dt = run([py, str(ENGINE_TESTS / "bench_service.py"), str(tree)], env)
            tail(stdout, 6)
            result["service"] = trailing_json(stdout)
            result["service_wall_s"] = round(dt, 1)

        sweep = [int(x) for x in args.walker_threads.split(",") if x.strip()]
        if sweep:
            result["walker_sweep"] = {}
            for n in sweep:
                print(f"[sweep] FFS_WALKER_THREADS={n}", flush=True)
                stdout, _dt = run([py, str(ENGINE_TESTS / "bench_service.py"), str(tree), "--only=index"],
                                  dict(env, FFS_WALKER_THREADS=str(n)))
                r = trailing_json(stdout)
                result["walker_sweep"][str(n)] = {k: r.get(k) for k in (
                    "fresh_index_entries", "fresh_index_s", "fresh_index_rate", "spawn_to_ready_empty_ms")}
                print(f"    {n} threads: {r.get('fresh_index_rate', 0):,.0f} entries/s", flush=True)

        # numbers the CI job checks
        head = {}
        loc, svc = result.get("local"), result.get("service")
        if loc:
            head["entries"] = loc.get("entries")
            head["search_p95_ms"] = loc["search"]["p95"]
            head["search_ge3_p95_ms"] = loc["search"]["ge3_p95"]
            head["rss_index_mb"] = loc.get("rss_index_mb")
            head["index_walk_entries_per_s"] = loc.get("entries_per_s")
            head["snapshot_load_s"] = loc.get("snapshot_load_s")
            ex = loc.get("exactness")
            if ex is not None:
                head["exactness_mismatches"] = len(ex["mismatches"])
        if svc:
            head["index_entries_per_s"] = svc.get("fresh_index_rate")
            head["spawn_to_ready_empty_ms"] = svc.get("spawn_to_ready_empty_ms")
        if "walker_sweep" in result:
            best = max(result["walker_sweep"].items(), key=lambda kv: kv[1].get("fresh_index_rate") or 0)
            head["best_walker_threads"] = int(best[0])
        result["headline"] = head

        out = Path(args.out) if args.out else REPO / "dist" / "bench" / f"engine-{sys.platform}-s{args.scale}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print("\nheadline:", json.dumps(head))
        print(f"wrote {out}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
