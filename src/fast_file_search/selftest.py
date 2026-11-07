"""Packaged self-tests (design G.9): ``--selftest <fixture_dir> <out.json>`` and ``--selftest-gui``.

``run_selftest`` never creates a QApplication. It starts the engine through the real
``multiprocessing`` spawn path (or in-process with ``--engine-inprocess``), indexes the
fixture, compares search counts with an ``os.walk`` ground truth and writes a JSON report.
Everything is imported lazily so the entry modules stay light.
"""
from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

READY_TIMEOUT = 60.0
INDEX_TIMEOUT = 180.0
SEARCH_TIMEOUT = 20.0


def sandbox_env(root: Path) -> None:
    """Point every per-user directory at ``root`` so a self-test never touches real data.
    Must run before the engine is spawned (the child inherits the environment)."""
    for name, sub in (
        ("FAST_FILE_SEARCH_CACHE_DIR", "cache"), ("APPDATA", "appdata"),
        ("LOCALAPPDATA", "localappdata"), ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_CACHE_HOME", "xdg-cache"), ("XDG_DATA_HOME", "xdg-data"),
        ("XDG_STATE_HOME", "xdg-state"),
    ):
        p = root / sub
        p.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(p)


class _Channel:
    """Uniform send/recv over a spawned process (Pipe) or an in-process Engine."""

    def __init__(self, boot, in_process: bool):
        self.in_process = in_process
        self.boot = boot
        self.proc = None
        self.conn = None
        self.engine = None
        self._q: "queue.Queue[Any]" = queue.Queue()
        if in_process:
            from .engine.service import Engine
            self.engine = Engine(boot, self._q.put)
        else:
            import multiprocessing
            from .engine.service import engine_main
            ctx = multiprocessing.get_context("spawn")
            self.conn, child = ctx.Pipe(duplex=True)
            self.proc = ctx.Process(target=engine_main, args=(child, boot), daemon=True)

    def start(self) -> None:
        if self.engine is not None:
            self.engine.start()
        else:
            self.proc.start()

    def send(self, msg) -> None:
        if self.engine is not None:
            self.engine.handle(msg)
        else:
            self.conn.send(msg)

    def recv(self, timeout: float):
        """Next message or None on timeout. Raises EOFError if the engine died."""
        if self.engine is not None:
            try:
                return self._q.get(timeout=timeout)
            except queue.Empty:
                return None
        if self.conn.poll(timeout):
            return self.conn.recv()
        if not self.proc.is_alive():
            raise EOFError(f"engine exited with code {self.proc.exitcode}")
        return None

    def stop(self) -> Optional[int]:
        from .engine.protocol import Shutdown
        try:
            self.send(Shutdown())
        except Exception:
            pass
        if self.engine is not None:
            self.engine.stop()
            return 0
        self.proc.join(5)
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(2)
        return self.proc.exitcode


def ground_truth(fixture: Path, excludes: Tuple[str, ...]) -> List[Tuple[str, bool]]:
    """(name, is_dir) for everything below ``fixture``, skipping excluded names
    (names are matched case-insensitively, as on Windows and macOS)."""
    skip = {e.casefold() for e in excludes}
    out: List[Tuple[str, bool]] = []
    for dirpath, dirnames, filenames in os.walk(fixture):
        dirnames[:] = [d for d in dirnames if d.casefold() not in skip]
        out.extend((d, True) for d in dirnames)
        out.extend((f, False) for f in filenames if f.casefold() not in skip)
    return out


def _pick_terms(entries: List[Tuple[str, bool]]) -> List[str]:
    """A few search terms that exist in the fixture (3+ ASCII letters from names)."""
    import re
    counts: Dict[str, int] = {}
    for name, _ in entries:
        for tok in re.findall(r"[a-z]{3,}", name.lower()):
            counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts, key=lambda t: (-counts[t], t))
    return ranked[:4]


def run_selftest(fixture_dir: str, out_json: str, *, in_process: bool = False) -> int:
    t_start = time.perf_counter()
    report: Dict[str, Any] = {
        "ok": False,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)) or "__compiled__" in globals(),
        "engine_inprocess": in_process,
        "checks": [],
        "timings": {},
    }
    try:
        import sqlite3
        report["sqlite_version"] = sqlite3.sqlite_version
    except Exception as exc:  # pragma: no cover
        report["sqlite_version"] = f"unavailable: {exc}"

    def check(name: str, ok: bool, detail: str = "") -> bool:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    sandbox = Path(tempfile.mkdtemp(prefix="ffs-selftest-"))
    chan: Optional[_Channel] = None
    try:
        fixture = Path(fixture_dir).resolve()
        if not check("fixture exists", fixture.is_dir(), str(fixture)):
            raise RuntimeError("fixture directory not found")
        sandbox_env(sandbox)
        from .config import DEFAULT_EXCLUDES, AppConfig, build_engine_bootstrap
        from .engine import protocol as P

        cfg = AppConfig(index_paths=[str(fixture)])
        boot = build_engine_bootstrap(cfg)
        entries = ground_truth(fixture, tuple(cfg.exclude_patterns) or DEFAULT_EXCLUDES)
        report["fixture_entries"] = len(entries)

        chan = _Channel(boot, in_process)
        t0 = time.perf_counter()
        chan.start()

        ready = _wait_for(chan, P.Ready, READY_TIMEOUT)
        report["timings"]["spawn_to_ready_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        if not check("engine ready", ready is not None, "no Ready message"):
            raise RuntimeError("engine did not become ready")
        report["capabilities"] = list(ready.capabilities)
        qt_mods = _qt_module_count(ready.capabilities)
        report["child_qt_modules"] = qt_mods
        check("engine has no Qt modules", qt_mods in (0, None),
              "not reported by the engine" if qt_mods is None else str(qt_mods))

        t1 = time.perf_counter()
        status = _wait_idle(chan, ready.status, INDEX_TIMEOUT)
        index_s = time.perf_counter() - t1
        report["timings"]["index_ms"] = round(index_s * 1000, 1)
        if status is not None and index_s > 0:
            report["timings"]["entries_per_s"] = round(status.total_entries / index_s)
        indexed = status.total_entries if status is not None else -1
        report["indexed_entries"] = indexed
        check("index reached idle", status is not None, "timed out")
        check("indexed entry count matches os.walk", indexed == len(entries),
              f"engine={indexed} walk={len(entries)}")

        gen = 0
        searches: List[Dict[str, Any]] = []

        def run(text: str, filters, expected: int, label: str) -> None:
            nonlocal gen
            gen += 1
            t = time.perf_counter()
            chan.send(P.SearchRequest(gen=gen, text=text, filters=filters, limit=50,
                                      suggestions=False, root_counts=False))
            res = _wait_for(chan, P.SearchResult, SEARCH_TIMEOUT, lambda m, g=gen: m.gen == g)
            ms = (time.perf_counter() - t) * 1000
            got = res.total if res is not None else None
            ok = res is not None and res.error is None and got == expected
            searches.append({"label": label, "expected": expected, "got": got, "ms": round(ms, 1)})
            check(f"search {label}", ok, f"expected={expected} got={got}"
                  + (f" error={res.error}" if res is not None and res.error else ""))

        # An empty text with empty filters is "no query" (0 rows, the UI's start state),
        # so "everything" is asked for as files + folders.
        everything = P.FilterState(kinds=frozenset({"file", "folder"}))
        run("", everything, len(entries), "all entries")
        run("", P.FilterState(kinds=frozenset({"folder"})),
            sum(1 for _, d in entries if d), "folders only")
        for term in _pick_terms(entries):
            run(term, P.FilterState(), sum(1 for n, _ in entries if term in n.lower()),
                f"term {term!r}")
        run("zzqqxx-no-such-name", P.FilterState(), 0, "no match")
        report["searches"] = searches
    except Exception as exc:
        check("selftest completed", False, f"{type(exc).__name__}: {exc}")
        report["traceback"] = traceback.format_exc()
    finally:
        if chan is not None:
            try:
                report["engine_exit_code"] = chan.stop()
            except Exception as exc:
                report["engine_exit_code"] = f"error: {exc}"
        shutil.rmtree(sandbox, ignore_errors=True)

    report["ok"] = all(c["ok"] for c in report["checks"]) and bool(report["checks"])
    report["timings"]["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
    try:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"selftest: cannot write {out_json}: {exc}", file=sys.stderr)
        report["ok"] = False
    for c in report["checks"]:
        if not c["ok"]:
            print(f"selftest FAIL: {c['name']}: {c['detail']}", file=sys.stderr)
    print("selftest", "OK" if report["ok"] else "FAILED", flush=True)
    return 0 if report["ok"] else 1


def _qt_module_count(capabilities) -> Optional[int]:
    """The engine reports ``qt_modules:<n>`` in ``Ready.capabilities``."""
    for cap in capabilities or ():
        if isinstance(cap, str) and cap.startswith("qt_modules:"):
            try:
                return int(cap.split(":", 1)[1])
            except ValueError:
                return None
    return None


def _wait_for(chan: _Channel, kind, timeout: float, pred=None):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        msg = chan.recv(min(remaining, 0.5))
        if msg is None:
            continue
        if type(msg).__name__ == "Fatal":
            raise RuntimeError(f"engine fatal: {msg.text}\n{msg.traceback}")
        if isinstance(msg, kind) and (pred is None or pred(msg)):
            return msg


def _status_done(status) -> bool:
    return (status.state == "idle" and bool(status.roots)
            and all(r.state in ("ok", "missing", "denied", "error") for r in status.roots))


def _wait_idle(chan: _Channel, first_status, timeout: float):
    from .engine import protocol as P
    if first_status is not None and _status_done(first_status):
        return first_status
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        st = _wait_for(chan, P.IndexStatus, remaining)
        if st is None:
            return None
        if _status_done(st):
            return st


# ---------------------------------------------------------------- GUI self-test

def run_selftest_gui(*, in_process: bool = False) -> int:
    """Create the real window offscreen, wait for ``Ready``, render one search, quit."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sandbox = Path(tempfile.mkdtemp(prefix="ffs-selftest-gui-"))
    code = 1
    try:
        sandbox_env(sandbox)
        fixture = sandbox / "fixture"
        (fixture / "docs").mkdir(parents=True)
        for name in ("alpha.txt", "beta.md", "docs/gamma.txt"):
            (fixture / name).write_text("selftest", encoding="utf-8")

        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication

        from .config import AppConfig, ConfigManager, build_engine_bootstrap
        from .engine_client import EngineClient
        from .engine.protocol import FilterState, SortSpec
        from .ui.main_window import MainWindow
        from .utils.platform_utils import redirect_qt_settings

        redirect_qt_settings()   # ui.ini goes to the sandbox too
        app = QApplication.instance() or QApplication(sys.argv[:1])
        cm = ConfigManager()
        cm.config = AppConfig(index_paths=[str(fixture)])
        cm.save()
        client = EngineClient(build_engine_bootstrap(cm.config), in_process=in_process)
        window = MainWindow(client, cm, first_run=False)
        window.show()
        app.processEvents()
        state: Dict[str, Any] = {"ready": False, "result": None, "gen": None}
        loop = QEventLoop()

        def on_status(st) -> None:
            # Search only once the first scan of the fixture is done: "Ready" means the
            # engine is up, not that the fixture is indexed yet.
            if state["gen"] is None and st.state == "idle" and st.total_entries > 0:
                state["gen"] = client.search("", FilterState(kinds=frozenset({"file", "folder"})), SortSpec())

        def on_result(r) -> None:
            # Ignore results for requests the window sent itself.
            if state["gen"] is not None and r.gen == state["gen"]:
                state["result"] = r
                loop.quit()

        client.ready.connect(lambda r: (state.update(ready=True), on_status(r.status) if r.status else None))
        client.index_status.connect(on_status)
        client.search_result.connect(on_result)
        QTimer.singleShot(int(READY_TIMEOUT * 1000), loop.quit)
        client.start()
        loop.exec()
        app.processEvents()
        pixmap = window.grab()
        result = state["result"]
        ok = state["ready"] and result is not None and result.total > 0 \
            and not pixmap.isNull() and pixmap.width() > 0
        print(f"selftest-gui ready={state['ready']} total={getattr(result, 'total', None)} "
              f"window={pixmap.width()}x{pixmap.height()}", flush=True)
        client.shutdown()
        window.close()
        code = 0 if ok else 1
    except Exception:
        traceback.print_exc()
        code = 1
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    print("selftest-gui", "OK" if code == 0 else "FAILED", flush=True)
    return code
