#!/usr/bin/env python
"""CI helper: summarize the bench JSONs into $GITHUB_STEP_SUMMARY and warn on thresholds.

    python benchmarks/ci_summary.py bench-out <runner-label>

Thresholds (warnings only for now, to become failures later), on CI runners:
    index rate >= 50,000 entries/s, search p95 <= 100 ms, GUI heartbeat p99 <= 40 ms.
Missing files or keys are reported as warnings, never raised, so the summary always renders.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

INDEX_MIN = 50_000
SEARCH_P95_MAX = 100.0
HEARTBEAT_P99_MAX = 40.0


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def num(v, fmt="{:,.0f}"):
    return "n/a" if v is None else fmt.format(v)


def main(argv) -> int:
    folder, label = Path(argv[1]), (argv[2] if len(argv) > 2 else "runner")
    eng = load(next(iter(folder.glob("bench-engine-*.json")), folder / "missing"))
    app = load(next(iter(folder.glob("bench-app-*.json")), folder / "missing"))
    eh = (eng or {}).get("headline", {})
    ah = (app or {}).get("headline", {})
    rows = []          # (metric, value text, threshold text, status)
    warnings = []

    def check(metric, value, limit, kind, unit=""):
        if value is None:
            warnings.append(f"{label}: {metric} was not measured")
            rows.append((metric, "n/a", f"{limit:,}{unit}", "missing"))
            return
        bad = value < limit if kind == "min" else value > limit
        sign = ">=" if kind == "min" else "<="
        rows.append((metric, num(value, "{:,.1f}") + unit, f"{sign} {limit:,}{unit}", "WARN" if bad else "ok"))
        if bad:
            warnings.append(f"{label}: {metric} = {value:,.1f}{unit}, expected {sign} {limit:,}{unit}")

    check("index rate, engine process (entries/s)", eh.get("index_entries_per_s"), INDEX_MIN, "min")
    check("index rate, full app (entries/s)", ah.get("index_entries_per_s"), INDEX_MIN, "min")
    check("search p95, engine matrix", eh.get("search_p95_ms"), SEARCH_P95_MAX, "max", " ms")
    check("search p95, through the client", ah.get("search_p95_ms"), SEARCH_P95_MAX, "max", " ms")
    check("GUI heartbeat p99", ah.get("heartbeat_p99_ms"), HEARTBEAT_P99_MAX, "max", " ms")
    if ah and not ah.get("watcher_burst_ok", True):
        warnings.append(f"{label}: the watcher burst was not fully searchable within 120 s")
    if eng and eh.get("exactness_mismatches"):
        warnings.append(f"{label}: {eh['exactness_mismatches']} exactness mismatches against the reference evaluator")

    out = [f"### Benchmarks: {label}", ""]
    meta = (app or eng or {}).get("meta", {})
    if meta:
        out.append(f"{meta.get('platform', '')} {meta.get('machine', '')}, Python {meta.get('python', '')}, "
                   f"sqlite {meta.get('sqlite', '')}, {meta.get('cpu_count', '?')} CPUs, "
                   f"{num(ah.get('entries') or eh.get('entries'))} entries")
        out.append("")
    out += ["| Metric | Value | Threshold | Status |", "|---|---|---|---|"]
    out += [f"| {m} | {v} | {t} | {s} |" for m, v, t, s in rows]
    out += ["", "| Other | Value |", "|---|---|",
            f"| startup to first paint | {num(ah.get('startup_first_paint_ms'))} ms |",
            f"| startup to engine Ready (warm index) | {num(ah.get('startup_ready_ms'))} ms |",
            f"| keystroke to results p95 (120 ms debounce included) | {num(ah.get('keystroke_to_results_p95_ms'))} ms |",
            f"| GUI process RSS | {num(ah.get('gui_rss_mb'))} MB |",
            f"| engine process RSS | {num(ah.get('engine_rss_mb'))} MB |",
            f"| index memory (bench_local) | {num(eh.get('rss_index_mb'))} MB |",
            f"| snapshot load | {num(eh.get('snapshot_load_s'), '{:.2f}')} s |"]
    sweep = (eng or {}).get("walker_sweep")
    if sweep:
        out += ["", "| Walker threads | Fresh index (entries/s) |", "|---|---|"]
        out += [f"| {n} | {num(d.get('fresh_index_rate'))} |" for n, d in sweep.items()]
        out.append(f"\nBest: {eh.get('best_walker_threads')} threads.")
    text = "\n".join(out) + "\n"
    print(text)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    for w in warnings:
        print(f"::warning::{w}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
