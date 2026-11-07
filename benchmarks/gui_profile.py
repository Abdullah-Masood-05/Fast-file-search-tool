"""GUI-thread profiling for ``bench_app.py --profile-gui DIR`` (imported only by the benchmark child).

Three tools, all opt-in and only active in a profile run:

* ``Probe``: timestamps (``time.perf_counter``, one clock for every thread) at each step of a
  keystroke: key handler, debounce fire, request send, result read by the client's reader
  thread, result delivered on the GUI thread, model apply, results-view paint. ``keystroke_phases``
  turns them into per-word phase durations.
* ``StallSampler``: a thread that, while the 16 ms heartbeat is more than 30 ms late, samples the
  GUI thread's Python stack (``sys._current_frames``) and counts the stacks. A stack whose
  innermost frame is the benchmark driver means the GUI thread was inside Qt's C++ code (event
  loop, painting, sleeping) with no Python frame running.
* ``cprofile_top`` / ``native_sample``: cProfile tables and, on macOS, ``/usr/bin/sample`` native
  stacks of the GUI thread.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict

LATE_MS = 30.0          # a heartbeat later than this is a stall
HEARTBEAT_MS = 16


def _pct(xs, p):
    if not xs:
        return None
    import math
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(p / 100 * len(xs)) - 1))]


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "p50": round(_pct(xs, 50), 2), "p95": round(_pct(xs, 95), 2),
            "max": round(max(xs), 2), "mean": round(sum(xs) / len(xs), 2)}


# --------------------------------------------------------------------------- probe
class Probe:
    def __init__(self):
        self.events = []            # (t, kind, data); list.append is atomic, so any thread may mark
        self.phase = "setup"

    def mark(self, kind, **data):
        self.events.append((time.perf_counter(), kind, data))

    def install(self, P):
        """Wrap the app's methods (class level, before the window exists)."""
        import fast_file_search.engine_client as ec
        import fast_file_search.ui.results as results_mod
        import fast_file_search.ui.search_bar as sb
        probe = self

        orig_edit = sb.SearchBar._on_text_edited

        def _on_text_edited(self, text):
            probe.mark("edit_start")
            orig_edit(self, text)
            probe.mark("edit_end")
        sb.SearchBar._on_text_edited = _on_text_edited

        orig_fire = sb.SearchBar._fire_debounced

        def _fire_debounced(self):
            probe.mark("debounce", text=self.field.text())
            orig_fire(self)
            probe.mark("debounce_end")
        sb.SearchBar._fire_debounced = _fire_debounced

        def reader_run(self):
            conn = self._conn
            try:
                while True:
                    msg = conn.recv()
                    if isinstance(msg, P.SearchResult):
                        probe.mark("recv", gen=msg.gen, offset=msg.offset, refresh=msg.refresh,
                                   rows=len(msg.rows), engine_ms=msg.elapsed_ms)
                    self.received.emit(self._epoch, msg)
            except (EOFError, OSError):
                pass
            except Exception:
                ec._log.exception("engine reader failed")
            self.closed.emit(self._epoch)
        ec._Reader.run = reader_run

        orig_msg = ec.EngineClient._on_message

        def _on_message(self, epoch, msg):
            if isinstance(msg, P.SearchResult):
                probe.mark("gui_recv", gen=msg.gen, offset=msg.offset, refresh=msg.refresh)
            elif isinstance(msg, P.IndexStatus):
                probe.mark("gui_status")
            orig_msg(self, epoch, msg)
        ec.EngineClient._on_message = _on_message

        view_cls = results_mod.ResultsView
        base_paint = view_cls.__mro__[1].paintEvent

        def paintEvent(self, event):  # noqa: N802
            t = time.perf_counter()
            base_paint(self, event)
            probe.events.append((t, "paint", {"ms": (time.perf_counter() - t) * 1000,
                                              "w": event.rect().width(), "h": event.rect().height()}))
        view_cls.paintEvent = paintEvent

    # hooks called from the Window subclass in bench_app
    def send(self, raw, gen, ms):
        self.mark("send", text=raw, gen=gen, ms=ms)

    def apply(self, gen, offset, refresh, ms):
        self.mark("apply", gen=gen, offset=offset, refresh=refresh, ms=ms)


def keystroke_phases(events, words):
    """``words``: [(word, t_first_key, t_last_key, t_done)] from the driver. Returns per-phase lists (ms)."""
    ph = defaultdict(list)
    ev = sorted(events, key=lambda e: e[0])
    for word, t_first, t_last, t_done in words:
        win = [e for e in ev if t_first - 0.001 <= e[0] <= t_done + 0.5]
        sends = [e for e in win if e[1] == "send"]
        final = [e for e in sends if e[2].get("text") == word]
        ph["requests_per_word"].append(len(sends))
        ph["stale_requests_per_word"].append(len(sends) - len(final[:1]))
        if not final:
            continue
        s = final[0]
        gen = s[2]["gen"]
        deb = [e for e in win if e[1] == "debounce" and e[0] <= s[0] and e[2].get("text") == word]
        if deb:
            ph["last_key_to_debounce_fire"].append((deb[-1][0] - t_last) * 1000)
        ph["send_handler"].append(s[2]["ms"])
        recv = next((e for e in win if e[1] == "recv" and e[2]["gen"] == gen and e[2]["offset"] == 0
                     and not e[2]["refresh"]), None)
        gui = next((e for e in win if e[1] == "gui_recv" and e[2]["gen"] == gen and e[2]["offset"] == 0
                    and not e[2]["refresh"]), None)
        app = next((e for e in win if e[1] == "apply" and e[2]["gen"] == gen and e[2]["offset"] == 0
                    and not e[2]["refresh"]), None)
        if recv:
            ph["send_to_reader_recv"].append((recv[0] - s[0]) * 1000)
            ph["engine_search_ms"].append(recv[2]["engine_ms"])
        if recv and gui:
            ph["reader_to_gui_delivery"].append((gui[0] - recv[0]) * 1000)
        if app:
            ph["model_apply"].append(app[2]["ms"])
            if gui:
                ph["gui_recv_to_apply_end"].append((app[0] - gui[0]) * 1000)
            # No view paint when the result is "no matches" (the empty state shows instead).
            paint = next((e for e in ev if e[1] == "paint" and app[0] <= e[0] <= t_done + 0.1), None)
            if paint is None:
                ph["no_view_paint"].append(1)
            else:
                ph["apply_to_first_paint"].append((paint[0] - app[0]) * 1000)
                ph["first_paint_ms"].append(paint[2]["ms"])
                ph["last_key_to_painted"].append((paint[0] + paint[2]["ms"] / 1000 - t_last) * 1000)
            ph["last_key_to_applied"].append((app[0] - t_last) * 1000)
    edits = [e for e in ev if e[1] in ("edit_start", "edit_end")]
    for a, b in zip(edits, edits[1:]):
        if a[1] == "edit_start" and b[1] == "edit_end":
            ph["key_handler"].append((b[0] - a[0]) * 1000)
    paints = [e[2]["ms"] for e in ev if e[1] == "paint"]
    ph["all_paints_ms"] = paints
    return {k: stats(v) for k, v in ph.items()}


# --------------------------------------------------------------------------- stall sampler
def _fmt_stack(frame, depth=14):
    out = []
    while frame is not None and len(out) < depth:
        co = frame.f_code
        fn = co.co_filename.replace("\\", "/")
        for marker in ("/site-packages/", "/src/", "/benchmarks/", "/lib/python3"):
            i = fn.rfind(marker)
            if i >= 0:
                fn = fn[i + len(marker):]
                break
        out.append(f"{fn}:{frame.f_lineno} {co.co_name}")
        frame = frame.f_back
    return tuple(out)


class StallSampler(threading.Thread):
    def __init__(self, gui_ident, hb, probe, period=0.004):
        super().__init__(name="bench-stall-sampler", daemon=True)
        self.gui_ident = gui_ident
        self.hb = hb
        self.probe = probe
        self.period = period
        self.samples = defaultdict(Counter)       # phase -> stack -> samples
        self.first = defaultdict(Counter)         # phase -> stack -> stalls (first sample of each stall)
        self.innermost = defaultdict(Counter)     # phase -> innermost frame -> samples
        self.stalls = Counter()
        self.paused = False
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        threshold = (HEARTBEAT_MS + LATE_MS) / 1000
        seen_last = None
        while not self._stop.wait(self.period):
            if self.paused:
                continue
            last = self.hb.last_pc
            if not last or time.perf_counter() - last <= threshold:
                continue
            frame = sys._current_frames().get(self.gui_ident)
            if frame is None:
                continue
            stack = _fmt_stack(frame)
            phase = self.probe.phase
            self.samples[phase][stack] += 1
            self.innermost[phase][stack[0] if stack else "?"] += 1
            if last != seen_last:
                seen_last = last
                self.stalls[phase] += 1
                self.first[phase][stack] += 1

    def report(self, top=12):
        out = {}
        for phase in self.samples:
            out[phase] = {
                "stalls": self.stalls[phase],
                "samples": sum(self.samples[phase].values()),
                "period_ms": self.period * 1000,
                "innermost": [[k, v] for k, v in self.innermost[phase].most_common(top)],
                "stacks": [[v, list(k)] for k, v in self.samples[phase].most_common(top)],
                "first_sample_stacks": [[v, list(k)] for k, v in self.first[phase].most_common(6)],
            }
        return out


# --------------------------------------------------------------------------- cProfile
_OTHER_THREADS = ("multiprocessing/connection.py", "queue.py", "threading.py", "gui_profile.py")
# Blocking calls of the reader/sender threads (cProfile on 3.12+ sees every thread).
_OTHER_FUNCS = ("<method 'acquire' of '_thread.lock' objects>", "<built-in method _winapi.ReadFile>",
                "<built-in method _winapi.WaitForMultipleObjects>", "<built-in method _winapi.PeekNamedPipe>",
                "<method 'GetOverlappedResult' of '_winapi.Overlapped' objects>",
                "<built-in method _winapi.WriteFile>", "<built-in method posix.read>",
                "<built-in method posix.write>", "<built-in method _pickle.loads>",
                "<built-in method _pickle.dumps>")


def cprofile_top(pr, n=60):
    import pstats
    st = pstats.Stats(pr)
    rows = []
    for (fn, line, func), (cc, nc, tt, ct, _callers) in st.stats.items():
        if func in _OTHER_FUNCS or any(x in fn.replace("\\", "/") for x in _OTHER_THREADS):
            continue         # the reader/sender threads and the sampler, not the GUI thread
        short = fn.replace("\\", "/")
        for marker in ("/site-packages/", "/src/", "/benchmarks/"):
            i = short.rfind(marker)
            if i >= 0:
                short = short[i + len(marker):]
                break
        rows.append({"func": f"{short}:{line}({func})", "ncalls": nc, "tottime_ms": round(tt * 1000, 2),
                     "cumtime_ms": round(ct * 1000, 2), "per_call_us": round(tt / max(1, nc) * 1e6, 2)})
    return {"total_tt_ms": round(sum(r["tottime_ms"] for r in rows), 1),
            "by_tottime": sorted(rows, key=lambda r: -r["tottime_ms"])[:n],
            "by_cumtime": sorted(rows, key=lambda r: -r["cumtime_ms"])[:n]}


# --------------------------------------------------------------------------- macOS native sampling
def start_native_sample(seconds, path):
    if sys.platform != "darwin":
        return None
    for cmd in (["sample", str(os.getpid()), str(int(seconds)), "1", "-mayDie", "-file", path],
                ["sudo", "-n", "sample", str(os.getpid()), str(int(seconds)), "1", "-mayDie", "-file", path]):
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError:
            continue
        time.sleep(1.0)
        if p.poll() is None or p.returncode == 0:
            return p
    return None


def native_main_thread_summary(path, min_frac=0.02, max_lines=160):
    """The main thread's call graph from a ``sample`` report, lines with >= ``min_frac`` of its samples."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return "(no sample report)"
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if "com.apple.main-thread" in l or re.match(r"\s*\d+ Thread_\d+\s+DispatchQueue_1", l)), None)
    if start is None:
        return "(main thread not found in the sample report)"
    m = re.match(r"\s*(\d+)", lines[start])
    total = int(m.group(1)) if m else 1
    out = [lines[start].strip()]
    for l in lines[start + 1:]:
        if re.match(r"\s*\d+ Thread_", l) or not l.strip():
            break
        m = re.match(r"^([\s+!:|]*)(\d+)\s+(.*)$", l)
        if not m:
            continue
        if int(m.group(2)) >= total * min_frac:
            out.append((m.group(1) + m.group(2) + " " + m.group(3))[:220])
        if len(out) >= max_lines:
            out.append("...")
            break
    tos = []
    i = next((i for i, l in enumerate(lines) if l.startswith("Sort by top of stack")), None)
    if i is not None:
        tos = [l.rstrip() for l in lines[i:i + 30]]
    return "\n".join(out + ["", "(all threads)"] + tos)


# --------------------------------------------------------------------------- text report
def text_report(prof):
    L = []
    w = L.append
    w(f"GUI profile: {prof.get('platform')} qpa={prof.get('qpa')} python={prof.get('python')} qt={prof.get('qt')}")
    w("")
    w("== Passes (keystroke -> results incl. 120 ms debounce; heartbeat lateness while typing on the idle index)")
    for name, p in prof.get("passes", {}).items():
        w(f"  {name:<10} k->r {fmt(p.get('keystroke_to_results_ms'))}   heartbeat {fmt(p.get('heartbeat'))}"
          f"   wait-overshoot {fmt(p.get('wait_overshoot_ms'))}")
    for name, p in prof.get("passes", {}).items():
        w("")
        w(f"== Phases per keystroke ({name})")
        for k, v in p.get("phases", {}).items():
            w(f"  {k:<28} {fmt(v)}")
    for name, rep in prof.get("stalls", {}).items():
        w("")
        w(f"== Stalls ({name}): {rep['stalls']} stalls, {rep['samples']} samples every {rep['period_ms']:.0f} ms")
        w("  innermost frame while the heartbeat was late:")
        for k, v in rep["innermost"]:
            w(f"    {v:>5}  {k}")
        w("  most common stacks (innermost first):")
        for v, st in rep["stacks"][:6]:
            w(f"    {v:>5}  " + "  <-  ".join(st[:7]))
    cp = prof.get("cprofile")
    if cp:
        w("")
        w(f"== cProfile (GUI thread, cprofile pass), total tottime {cp['total_tt_ms']} ms")
        w("  by tottime:")
        for r in cp["by_tottime"]:
            w(f"    {r['tottime_ms']:>9.1f} ms  cum {r['cumtime_ms']:>9.1f}  n={r['ncalls']:<7} {r['func']}")
        w("  by cumtime:")
        for r in cp["by_cumtime"]:
            w(f"    {r['cumtime_ms']:>9.1f} ms  tot {r['tottime_ms']:>9.1f}  n={r['ncalls']:<7} {r['func']}")
    if prof.get("native"):
        w("")
        w("== Native sample (macOS /usr/bin/sample, main thread, native pass)")
        w(prof["native"])
    return "\n".join(L) + "\n"


def fmt(d):
    if not d or not d.get("n"):
        return "n/a"
    return f"p50 {d.get('p50')}  p95 {d.get('p95', d.get('p99'))}  max {d.get('max')}  n={d['n']}"
