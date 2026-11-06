"""protocol.py: every message pickles round-trip; the module is Appendix 1 verbatim;
no Qt or sqlite3 is pulled in by the engine side."""
import dataclasses
import os
import pickle
import re
import subprocess
import sys
from pathlib import Path

import pytest

from fast_file_search.engine import protocol as P

REPO = Path(__file__).resolve().parents[2]


def _samples():
    cfg = P.EngineConfig(roots=("C:\\a", "/b"), exclude_patterns=("*.tmp", "build/out"), include_home_library=True)
    boot = P.EngineBootstrap(cfg, "/c", "/d", "/l", own_paths=("/x",), log_level="DEBUG", app_version="2.0.0")
    flt = P.FilterState(kinds=frozenset({"image"}), exts=frozenset({"png"}), size_min=1, size_max=10,
                        mtime_min=1.5, mtime_max=2.5, root_ids=frozenset({3}), folder="/a", path_contains="src",
                        excludes=("node_modules",), favorites_only=True)
    row = P.ResultRow(1, "caf\udce9.txt", "/a/caf\udce9.txt", 3, "", 10, 20, P.HIDDEN, ((0, 3),), True)
    rs = P.RootStatus(3, "/a", "a", "ok", 10, 2, 100, 1, 5, 9, 1.5, "limited", 123)
    st = P.IndexStatus("scanning", (rs,), 10, 2, "limited", 7, "Waiting…")
    return [
        cfg, boot, flt, P.SortSpec("size", True), P.SearchRequest(5, "q", flt, P.SortSpec(), 50, False, False, True),
        P.FetchPage(5, 200, 500), row, P.Suggestion("history", "q", 2),
        P.SearchResult(5, 1, 0, (row,), 1.5, ((3, 1),), (P.Suggestion("name", "x", 1),), "qq", None, 7, True, True,
                       ("q",)),
        P.Hello(), P.Configure(cfg), P.IndexCommand("rescan", (3,)), rs, st,
        P.Ready(P.PROTOCOL_VERSION, st, ("snapshot",)), P.IndexChanged(8),
        P.UserOp("set_tags", ("/a",), ["t1", "t2"]), P.ItemInfoRequest(1, "/a"), P.ItemInfo(1, "/a", True, ("t",), "n"),
        P.Notice("warning", "tcc_denied", "denied", (("root_id", 3),)), P.Fatal("boom", "tb"), P.Shutdown(),
    ]


def _message_classes():
    out = []
    for name in dir(P):
        obj = getattr(P, name)
        if isinstance(obj, type) and obj.__module__ == P.__name__ and (
                dataclasses.is_dataclass(obj) or hasattr(obj, "_fields")):
            out.append(obj)
    return out


@pytest.mark.parametrize("msg", _samples(), ids=lambda m: type(m).__name__)
def test_pickle_round_trip(msg):
    for proto in (pickle.DEFAULT_PROTOCOL, pickle.HIGHEST_PROTOCOL):
        back = pickle.loads(pickle.dumps(msg, protocol=proto))
        assert back == msg and type(back) is type(msg)


def test_every_message_class_is_sampled():
    sampled = {type(m) for m in _samples()}
    assert set(_message_classes()) <= sampled


def test_dataclasses_are_frozen_and_hashable_where_needed():
    with pytest.raises(dataclasses.FrozenInstanceError):
        P.FilterState().size_min = 3
    assert P.FilterState().is_empty() and not P.FilterState(exts=frozenset({"x"})).is_empty()
    hash(P.FilterState(kinds=frozenset({"code"})))
    hash(P.SearchRequest(1, "x"))


def test_protocol_is_appendix_1_verbatim():
    design = (REPO / "dist" / "notes" / "design.md")
    if not design.exists():
        pytest.skip("design notes not present")
    text = design.read_text(encoding="utf-8")
    block = text.split("## Appendix 1", 1)[1]
    code = re.search(r"```python\n(.*?)\n```", block, re.S).group(1)
    src = (REPO / "src" / "fast_file_search" / "engine" / "protocol.py").read_text(encoding="utf-8")
    assert src.rstrip("\n") == code.rstrip("\n")


def _imported_modules(module: str):
    env = dict(os.environ)
    env.pop("FFS_ENGINE_CORE", None)
    code = (f"import sys, importlib; importlib.import_module({module!r}); "
            "print('\\n'.join(sorted(sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(REPO),
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return set(out.stdout.split())


@pytest.mark.parametrize("module", ["fast_file_search.engine.service", "fast_file_search.engine.walker",
                                    "fast_file_search.engine.watcher", "fast_file_search.engine.power",
                                    "fast_file_search.engine.macos", "fast_file_search.engine.exclusions",
                                    "fast_file_search.engine.protocol"])
def test_no_qt_in_engine(module):
    mods = _imported_modules(module)
    assert not any(m == "PySide6" or m.startswith("PySide6.") for m in mods)


def test_engine_client_imports_no_sqlite():
    mods = _imported_modules("fast_file_search.engine_client")
    assert "sqlite3" not in mods and "_sqlite3" not in mods


def test_engine_sources_do_not_import_qt():
    eng = REPO / "src" / "fast_file_search" / "engine"
    for f in ("service.py", "walker.py", "watcher.py", "exclusions.py", "power.py", "macos.py", "protocol.py"):
        src = (eng / f).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+PySide6", src, re.M), f
