"""Entry modules stay light (spawn re-runs their top level) and --selftest works."""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
ENTRY_FILES = [ROOT / "main.py", SRC / "fast_file_search" / "main.py"]
ALLOWED = {"sys", "os", "multiprocessing", "__future__"}


def top_level_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:  # module level only, including if/try blocks
        for sub in ast.walk(node) if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ():
            if isinstance(sub, ast.Import):
                names.update(a.name.split(".")[0] for a in sub.names)
            elif isinstance(sub, ast.ImportFrom):
                names.add(("." * sub.level) + (sub.module or ""))
    return names


@pytest.mark.parametrize("path", ENTRY_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_entry_modules_import_only_light_modules_at_top_level(path):
    assert top_level_imports(path) <= ALLOWED


@pytest.mark.parametrize("path", ENTRY_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_entry_modules_call_freeze_support_under_main_guard(path):
    text = path.read_text(encoding="utf-8")
    guard = text.index('if __name__ == "__main__":')
    assert "multiprocessing.freeze_support()" in text[guard:]


def test_importing_entry_modules_pulls_in_nothing_heavy():
    code = (
        "import sys, runpy\n"
        f"sys.path.insert(0, r'{SRC}')\n"
        f"runpy.run_path(r'{ROOT / 'main.py'}', run_name='not_main')\n"
        "import fast_file_search.main\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('PySide6', 'shiboken6', 'sqlite3', 'watchdog') or m in "
        "('fast_file_search.config', 'fast_file_search.ui', 'fast_file_search.engine_client')]\n"
        "print('BAD', bad)\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("BAD []"), out.stdout


def test_parse_args():
    from fast_file_search.main import parse_args

    a = parse_args(["--engine-inprocess", "--selftest", "fx", "out.json", "hello", "world"])
    assert a.selftest == ["fx", "out.json"] and a.engine_inprocess
    assert a.query == ["hello", "world"] and not a.selftest_gui
    assert parse_args(["--selftest-gui"]).selftest_gui


def make_fixture(root: Path) -> Path:
    (root / "docs" / "reports").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    for rel in ("alpha_report.txt", "docs/beta_report.md", "docs/reports/report_2024.pdf",
                "notes.txt", "node_modules/pkg/report.js"):
        (root / rel).write_text("x", encoding="utf-8")
    return root


class FakeEngine:
    """Minimal in-process stand-in for engine.service.Engine (os.walk + substring)."""

    def __init__(self, boot, send):
        from fast_file_search.engine import protocol as P
        self.P, self.boot, self.send = P, boot, send

    def _entries(self):
        skip = {e.casefold() for e in self.boot.config.exclude_patterns}
        out = []
        for root in self.boot.config.roots:
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if d.casefold() not in skip]
                out += [(d, True) for d in dns] + [(f, False) for f in fns]
        return out

    def _status(self):
        P = self.P
        n = len(self._entries())
        roots = tuple(P.RootStatus(1, r, "r", "ok", n, 0, 0, 0) for r in self.boot.config.roots)
        return P.IndexStatus("idle", roots, n, 0, "on", 1)

    def start(self):
        self.send(self.P.Ready(self.P.PROTOCOL_VERSION, self._status(), ("qt_modules:0",)))

    def stop(self, timeout=3.0):
        pass

    def handle(self, msg):
        P = self.P
        if isinstance(msg, P.SearchRequest):
            rows = [e for e in self._entries() if msg.text in e[0].lower()]
            if msg.filters.kinds == frozenset({"folder"}):
                rows = [e for e in rows if e[1]]
            self.send(P.SearchResult(msg.gen, len(rows), 0, (), 0.1))


def test_selftest_logic_with_fake_engine(tmp_path, monkeypatch):
    fake = types.ModuleType("fast_file_search.engine.service")
    fake.Engine = FakeEngine
    fake.engine_main = None
    monkeypatch.setitem(sys.modules, "fast_file_search.engine.service", fake)
    from fast_file_search.selftest import run_selftest

    out = tmp_path / "out.json"
    code = run_selftest(str(make_fixture(tmp_path / "fx")), str(out), in_process=True)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0, report
    assert report["ok"] and report["child_qt_modules"] == 0
    assert report["fixture_entries"] == 6  # node_modules is excluded
    assert report["indexed_entries"] == 6
    assert report["sqlite_version"] and report["platform"]
    assert {"spawn_to_ready_ms", "index_ms"} <= set(report["timings"])
    assert len(report["searches"]) >= 4


def test_selftest_reports_failure_for_missing_fixture(tmp_path):
    from fast_file_search.selftest import run_selftest

    out = tmp_path / "out.json"
    assert run_selftest(str(tmp_path / "nope"), str(out), in_process=True) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is False


@pytest.mark.slow
@pytest.mark.skipif(importlib.util.find_spec("fast_file_search.engine.service") is None,
                    reason="engine service (P2) not present yet")
def test_selftest_real_engine_unfrozen(tmp_path):
    out = tmp_path / "real.json"
    fixture = make_fixture(tmp_path / "fx")
    env = dict(os.environ, PYTHONPATH=str(SRC))
    proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--selftest", str(fixture),
                           str(out)], env=env, capture_output=True, text=True, timeout=240)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert proc.returncode == 0, (proc.stderr, report)
    assert report["child_qt_modules"] in (0, None)
