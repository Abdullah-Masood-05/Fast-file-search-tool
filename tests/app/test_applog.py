"""Rotating logs, crash hooks and faulthandler."""
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fast_file_search import applog

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.fixture
def logs(tmp_path):
    root = logging.getLogger()
    level = root.level
    applog.setup_logging("gui", tmp_path)
    yield tmp_path
    applog.shutdown_logging()
    root.setLevel(level)


def read_log(d: Path) -> str:
    for h in logging.getLogger().handlers:
        h.flush()
    return (d / "gui.log").read_text(encoding="utf-8")


def test_header_has_versions(logs):
    text = read_log(logs)
    assert "Fast File Search Pro" in text and "Python " in text
    assert "frozen=" in text and "PySide6" in text


def test_rotation_at_1mb_x5(logs):
    log = logging.getLogger("fast_file_search.test")
    line = "x" * 1000
    for i in range(9000):  # ~9 MB, enough for more than 5 rollovers
        log.info("%d %s", i, line)
    names = sorted(p.name for p in logs.iterdir() if p.name.startswith("gui.log"))
    assert names == ["gui.log", "gui.log.1", "gui.log.2", "gui.log.3", "gui.log.4", "gui.log.5"]
    for n in names:
        assert (logs / n).stat().st_size <= 1_000_000 + 2000


def test_setup_twice_does_not_duplicate(logs):
    applog.setup_logging("gui", logs)
    logging.getLogger("fast_file_search.t").info("once-marker")
    assert read_log(logs).count("once-marker") == 1


def test_sys_excepthook_logs_traceback_and_notifies(logs):
    seen = []
    applog.add_exception_listener(lambda t, e, tb, name: seen.append((t, str(e), name)))
    try:
        raise ValueError("boom-main")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    text = read_log(logs)
    assert "Traceback" in text and "ValueError: boom-main" in text
    assert seen == [(ValueError, "boom-main", "MainThread")]


def test_threading_excepthook_logs_traceback(logs):
    seen = []
    applog.add_exception_listener(lambda t, e, tb, name: seen.append(name))

    def work():
        raise RuntimeError("boom-thread")

    t = threading.Thread(target=work, name="worker-x")
    t.start()
    t.join()
    text = read_log(logs)
    assert "RuntimeError: boom-thread" in text and "worker-x" in text
    assert seen == ["worker-x"]


def test_failing_listener_is_contained(logs):
    def bad(*a):
        raise KeyError("listener")

    applog.add_exception_listener(bad)
    try:
        raise ValueError("x")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    assert "exception listener failed" in read_log(logs)


def test_qt_messages_map_to_logging_and_drop_noise(logs):
    class Ctx:
        category = "default"

    class Noise:
        category = "qt.qpa.fonts"

    class T:
        name = "QtWarningMsg"

    applog.qt_message_to_logging(T, Ctx, "real-warning")
    applog.qt_message_to_logging(T, Noise, "font-noise")
    text = read_log(logs)
    assert "real-warning" in text and "WARNING" in text
    assert "font-noise" not in text


def test_debug_env_switches_level(tmp_path, monkeypatch):
    monkeypatch.setenv("FFS_DEBUG", "1")
    root = logging.getLogger()
    level = root.level
    try:
        applog.setup_logging("gui", tmp_path)
        assert root.level == logging.DEBUG
    finally:
        applog.shutdown_logging()
        root.setLevel(level)


def test_faulthandler_writes_on_segfault(tmp_path):
    code = (
        "import faulthandler\n"
        "from pathlib import Path\n"
        "from fast_file_search import applog\n"
        f"applog.setup_logging('engine', Path(r'{tmp_path}'))\n"
        "faulthandler._sigsegv()\n"
    )
    env = dict(os.environ, PYTHONPATH=SRC)
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    crash = (tmp_path / "engine-crash.log").read_text(encoding="utf-8")
    assert "Fatal Python error" in crash and "most recent call first" in crash.lower()
    assert (tmp_path / "engine.log").exists()
