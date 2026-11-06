"""The real MainWindow with the real engine.

* responsiveness (slow): a separate engine process indexes a ~50k-entry tree while a key is
  typed every 90 ms; a 16 ms heartbeat on the GUI thread must never be late by much
  (p99 <= 30 ms and max <= 50 ms on a dev box; CI runners get p99 <= 40 ms, max 80 ms).
* filter-aware suggestions: with a Type or Path chip applied, every name suggestion must come
  from the filtered result space (this was a v1 bug: suggestions ignored the filters).
"""
import math
import os
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QElapsedTimer, Qt, QTimer
from PySide6.QtTest import QTest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "benchmarks"))

from fast_file_search import kinds as K  # noqa: E402
from fast_file_search.config import AppConfig, ConfigManager, build_engine_bootstrap  # noqa: E402
from fast_file_search.engine.textfold import fold  # noqa: E402
from fast_file_search.engine_client import EngineClient  # noqa: E402
from fast_file_search.ui.filter_chips import ChipState  # noqa: E402
from fast_file_search.ui.main_window import MainWindow  # noqa: E402
from fast_file_search.utils.platform_utils import redirect_qt_settings  # noqa: E402
from gen_tree import generate  # noqa: E402

ON_CI = bool(os.environ.get("CI"))
P99_LIMIT, MAX_LIMIT = (40.0, 80.0) if ON_CI else (30.0, 50.0)
WORDS = ["gifttop", "index", "readme", "lodash", "userprofile", "zzzqq", "button", "invoice"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("FFS_POWER", "ac")
    redirect_qt_settings()


def make_window(roots, in_process):
    cm = ConfigManager()
    cm.config = AppConfig(index_paths=[str(r) for r in roots])
    cm.save()
    client = EngineClient(build_engine_bootstrap(cm.config), in_process=in_process)
    win = MainWindow(client, cm, first_run=False)
    win.show()
    client.start()
    return win, client


def close(win):
    # Not close_and_quit(): that would end the QApplication shared by the whole test run.
    win.client.shutdown()
    win.close()


def wait_for(cond, timeout=60.0, step=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        QTest.qWait(step)
    return cond()


def is_idle(win, nroots):
    st = win._status
    return (st is not None and st.state == "idle" and len(st.roots) == nroots
            and all(r.state == "ok" and r.last_scan_end is not None for r in st.roots))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(p / 100 * len(xs)) - 1))]


class Heartbeat:
    def __init__(self, interval=16):
        self.interval = interval
        self.timer = QTimer()
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(interval)
        self.timer.timeout.connect(self._tick)
        self.clock = QElapsedTimer()
        self.late = []
        self.last = 0

    def start(self):
        self.clock.start()
        self.last = self.clock.nsecsElapsed()
        self.timer.start()

    def stop(self):
        self.timer.stop()

    def _tick(self):
        now = self.clock.nsecsElapsed()
        self.late.append(max(0.0, (now - self.last) / 1e6 - self.interval))
        self.last = now


@pytest.fixture(scope="module")
def tree50k(tmp_path_factory):
    root = tmp_path_factory.mktemp("tree50k") / "tree"
    generate(str(root), 47_000, 3_100, seed=11)
    return root


@pytest.mark.slow
def test_gui_stays_responsive_while_indexing_and_typing(qapp, tree50k):
    win, client = make_window([tree50k], in_process=False)
    hb = Heartbeat()
    try:
        assert wait_for(lambda: client.is_ready, 60)
        win.activateWindow()
        field = win.bar.field
        hb.start()
        deadline = time.monotonic() + 180
        t_start = time.monotonic()
        keys, keys_scanning, w = 0, 0, 0
        # Type until the scan is done and for at least 5 s in total (a 50k scan can finish in
        # about a second on a fast box; the rest checks typing against the freshly built index).
        while time.monotonic() < deadline and (not is_idle(win, 1) or time.monotonic() - t_start < 5.0):
            word = WORDS[w % len(WORDS)]
            w += 1
            field.setFocus()
            field.clear()
            for ch in word:
                scanning = not is_idle(win, 1)
                QTest.keyClicks(field, ch)
                keys += 1
                keys_scanning += scanning
                QTest.qWait(90)
            QTest.qWait(150)
        hb.stop()
        assert is_idle(win, 1), "index did not finish in time"
        total = win._status.total_entries
        assert total >= 40_000, total
        late = hb.late
        stats = {"n": len(late), "p99": round(pct(late, 99), 1), "max": round(max(late), 1), "keys": keys, "keys_while_scanning": keys_scanning}
        print("heartbeat lateness (ms):", stats, "entries:", total)
        assert keys >= 20 and keys_scanning >= 3, stats   # the typing really overlapped the scan
        assert stats["n"] >= 100, stats
        assert stats["p99"] <= P99_LIMIT, stats
        assert stats["max"] <= MAX_LIMIT, stats
    finally:
        hb.stop()
        close(win)


# ------------------------------------------------------------------ filter-aware suggestions
def _suggestion_tree(base: Path):
    a, b = base / "alpha", base / "beta"
    files = {
        a / "photos": ["giftbox.png", "giftcard.jpg", "giftwrap.png"],
        a / "docs": ["giftlist.txt", "giftnotes.md", "gifts.pdf"],
        b / "photos": ["giftphoto.png", "gifthero.jpg"],
        b / "code": ["giftshop.py", "gifttax.js", "giftutil.py"],
    }
    for d, names in files.items():
        d.mkdir(parents=True)
        for n in names:
            (d / n).write_text("x")
    return a, b


def _walk(roots):
    out = []
    for r in roots:
        for dp, dn, fn in os.walk(r):
            out += [(d, True, os.path.join(dp, d)) for d in dn]
            out += [(f, False, os.path.join(dp, f)) for f in fn]
    return out


def _kind(g):
    return K.kind_of(g[0], 1 if g[1] else 0)


def _type(win, text, timeout=15.0):
    f = win.bar.field
    f.setFocus()
    f.clear()
    QTest.keyClicks(f, text)
    ok = wait_for(lambda: win._last_result is not None and win._displayed_key == win._req_key
                  and win._req_raw == text, timeout)
    assert ok, f"no result for {text!r}"
    return win._last_result


def test_suggestions_respect_type_and_path_filters(qapp, tmp_path):
    a, b = _suggestion_tree(tmp_path / "t")
    win, _client = make_window([a, b], in_process=True)
    try:
        assert wait_for(lambda: is_idle(win, 2), 30)
        ground = _walk([a, b])
        root_b = [r.id for r in win._status.roots if Path(r.path) == b][0]

        res = _type(win, "gift")
        plain = [s.text for s in res.suggestions if s.kind == "name"]
        assert plain, "unfiltered search should offer name suggestions"

        cases = {
            "type=image": (ChipState(type_key="image"), lambda g: _kind(g) == "image"),
            "type=code": (ChipState(type_key="code"), lambda g: _kind(g) == "code"),
            "path=beta root": (ChipState(root_id=root_b), lambda g: g[2].startswith(str(b) + os.sep)),
            "path contains docs": (ChipState(path_contains="docs"),
                                   lambda g: "docs" in os.path.dirname(g[2]).lower()),
            "type=image + path=beta": (ChipState(type_key="image", root_id=root_b),
                                       lambda g: _kind(g) == "image" and g[2].startswith(str(b) + os.sep)),
        }
        saw_difference = False
        for label, (state, pred) in cases.items():
            win.chips.set_chip_state(state, emit=True)
            _type(win, "gif")           # a fresh request that carries the chip state
            res = _type(win, "gift")
            allowed = {fold(g[0]) for g in ground if pred(g)}
            names = [s for s in res.suggestions if s.kind == "name"]
            assert names, f"{label}: expected suggestions"
            bad = [s.text for s in names if fold(s.text) not in allowed]
            assert not bad, f"{label}: suggestions outside the filtered space: {bad}"
            saw_difference |= any(fold(n) not in allowed for n in plain)
            want_total = sum(1 for g in ground if "gift" in fold(g[0]) and pred(g))
            assert res.total == want_total, (label, res.total, want_total)
            for s in res.suggestions:
                if s.kind == "history":
                    assert s.count >= 1
        assert saw_difference, "the unfiltered suggestions never differed from the filtered ones: weak test"
        win.chips.clear()
    finally:
        close(win)
