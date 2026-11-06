"""Single-instance lock and activation through QLocalServer."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from fast_file_search.single_instance import SingleInstance, server_name

SRC = str(Path(__file__).resolve().parents[2] / "src")

SECOND = """
import sys, time
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from fast_file_search.single_instance import SingleInstance
app = QCoreApplication([])
t = time.perf_counter()
si = SingleInstance(sys.argv[2], Path(sys.argv[1]))
first = si.acquire()
print("ACQUIRE", first, round((time.perf_counter() - t) * 1000), flush=True)
sys.exit(0 if not first else 5)
"""


@pytest.fixture(scope="module")
def app(qapp):
    # pytest-qt's QApplication: a bare QCoreApplication here would outlive this module
    # and crash every later test that builds widgets.
    return qapp


def spawn_second(state: Path, query: str):
    env = dict(os.environ, PYTHONPATH=SRC)
    return subprocess.Popen([sys.executable, "-c", SECOND, str(state), query], env=env,
                            stdout=subprocess.PIPE, text=True)


def pump(app, until, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not until():
        app.processEvents()
        time.sleep(0.005)


def test_server_name_is_per_user_and_dir(tmp_path):
    a, b = server_name(tmp_path / "a"), server_name(tmp_path / "b")
    assert a.startswith("fast-file-search-pro-") and len(a) == len("fast-file-search-pro-") + 12
    assert a != b and a == server_name(tmp_path / "a")


def test_second_launch_activates_first_quickly(app, tmp_path, perf_scale):
    first = SingleInstance("", tmp_path)
    assert first.acquire() is True
    got = []
    first.activated.connect(got.append)
    try:
        proc = spawn_second(tmp_path, "report 2024")
        pump(app, lambda: proc.poll() is not None and bool(got))
        out = proc.stdout.read()
        assert proc.returncode == 0, out
        assert got == ["report 2024"]
        label, flag, ms = out.split()
        assert flag == "False" and int(ms) <= 500 * perf_scale
    finally:
        first.release()


def test_activation_without_query_emits_empty_string(app, tmp_path):
    first = SingleInstance("", tmp_path)
    assert first.acquire()
    got = []
    first.activated.connect(got.append)
    try:
        proc = spawn_second(tmp_path, "")
        pump(app, lambda: proc.poll() is not None and bool(got))
        proc.stdout.read()
        assert got == [""]
    finally:
        first.release()


def test_lock_released_allows_new_first_instance(app, tmp_path):
    a = SingleInstance("", tmp_path)
    assert a.acquire()
    a.release()
    b = SingleInstance("", tmp_path)
    try:
        assert b.acquire()
    finally:
        b.release()


def test_malformed_message_is_ignored(app, tmp_path):
    si = SingleInstance("", tmp_path)
    got = []
    si.activated.connect(got.append)
    si._handle(b"not json")
    si._handle(b'{"cmd": "other"}')
    si._handle(b'{"cmd": "activate", "query": 5}')
    assert got == [""]
