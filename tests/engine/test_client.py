"""EngineClient (GUI side): in-process and real spawn. Signals, stale-gen drops,
supervision (kill -> 'restarting' -> 'ready' with the search replayed, restart
limit -> 'failed'), clean shutdown, and the engine exiting <= 3 s after its
parent process is killed."""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from fake_core import make_boot, pid_alive, select_core
from fast_file_search import engine_client as EC
from fast_file_search.engine import protocol as P
from fast_file_search.engine_client import EngineClient

REPO = Path(__file__).resolve().parents[2]
slow = pytest.mark.slow


@pytest.fixture
def core(monkeypatch):
    kind = select_core(monkeypatch)
    monkeypatch.setenv("FFS_POWER", "ac")
    return kind


def _tree(tmp_path, n=50):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    for i in range(n):
        (root / ("sub" if i % 2 else "") / f"clientfile{i}.txt").write_text("x")
    return root


def _wait_result(qtbot, client, gen, timeout=10000, refresh=False):
    box = []

    def got(res):
        if res.gen == gen and res.refresh == refresh:
            box.append(res)
    client.search_result.connect(got)
    try:
        qtbot.waitUntil(lambda: bool(box), timeout=timeout)
    finally:
        client.search_result.disconnect(got)
    return box[0]


def _wait_idle(qtbot, client, timeout=15000):
    qtbot.waitUntil(lambda: client.last_status is not None and client.last_status.state == "idle"
                    and client.last_status.total_entries > 0, timeout=timeout)


def test_in_process_client(core, qtbot, tmp_path):
    root = _tree(tmp_path)
    client = EngineClient(make_boot(tmp_path / "app", [root]), in_process=True)
    states = []
    client.engine_state.connect(states.append)
    with qtbot.waitSignal(client.ready, timeout=10000) as ready:
        client.start()
    try:
        assert isinstance(ready.args[0], P.Ready) and client.is_ready
        _wait_idle(qtbot, client)
        gen = client.search("clientfile", limit=10)
        assert gen == client.current_gen == 1
        res = _wait_result(qtbot, client, gen)
        assert res.total == 50 and len(res.rows) == 10
        client.fetch_page(gen, 10, 20)
        page = []
        client.search_result.connect(lambda r: page.append(r) if r.offset == 10 else None)
        qtbot.waitUntil(lambda: bool(page), timeout=5000)
        assert len(page[0].rows) == 20
        f = str(root / "clientfile0.txt")
        client.user_op("favorite", (f,))
        with qtbot.waitSignal(client.item_info, timeout=5000) as info:
            rid = client.request_item_info(f)
        assert info.args[0].req_id == rid and info.args[0].favorite
        with qtbot.waitSignal(client.index_changed, timeout=10000):
            (root / "clientfile_new.txt").write_text("x")
    finally:
        client.shutdown()
    assert states[:2] == ["starting", "ready"] and client.state == "stopped"


def test_stale_generations_are_dropped(core, qtbot, tmp_path):
    client = EngineClient(make_boot(tmp_path / "app", []), in_process=True)
    got = []
    client.search_result.connect(got.append)
    client.current_gen = 5
    old = P.SearchResult(gen=4, total=1, offset=0, rows=(), elapsed_ms=0.0)
    new = P.SearchResult(gen=5, total=2, offset=0, rows=(), elapsed_ms=0.0)
    client._on_message(client._epoch, old)
    client._on_message(client._epoch, new)
    client._on_message(client._epoch - 1, new)          # from a previous engine incarnation
    assert got == [new]


def test_requests_before_ready_are_queued(core, qtbot, tmp_path):
    root = _tree(tmp_path, 10)
    client = EngineClient(make_boot(tmp_path / "app", [root]), in_process=True)
    client.start()
    try:
        gen = client.search("clientfile")                # may arrive before Ready
        res = _wait_result(qtbot, client, gen)
        assert res.gen == gen
    finally:
        client.shutdown()


@slow
def test_spawn_start_search_kill_restart(core, qtbot, tmp_path):
    root = _tree(tmp_path)
    client = EngineClient(make_boot(tmp_path / "app", [root]))
    states = []
    client.engine_state.connect(states.append)
    t0 = time.perf_counter()
    with qtbot.waitSignal(client.ready, timeout=20000) as ready:
        client.start()
    try:
        caps = ready.args[0].capabilities
        assert "qt_modules:0" in caps                    # the child never imports PySide6
        assert client.engine_pid and client.engine_pid != os.getpid()
        assert client.spawn_to_ready_ms is not None and (time.perf_counter() - t0) < 20
        _wait_idle(qtbot, client)
        gen = client.search("clientfile")
        assert _wait_result(qtbot, client, gen).total == 50
        pid = client.engine_pid
        client.process.kill()
        qtbot.waitUntil(lambda: "restarting" in states, timeout=10000)
        assert not client.is_ready
        replay = _wait_result(qtbot, client, gen, timeout=20000)   # replayed after the restart
        assert replay.total == 50
        assert states[-1] == "ready" and states.count("ready") == 2
        assert client.engine_pid != pid
        assert not pid_alive(pid)
    finally:
        proc = client.process
        client.shutdown(timeout=3.0)
    assert proc is not None and proc.exitcode == 0     # a clean exit, not terminate()
    assert client.state == "stopped" and states[-1] == "ready"


@slow
def test_restart_limit_reports_failed(core, qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(EC, "RESTART_DELAYS", (0.05, 0.05, 0.05))
    client = EngineClient(make_boot(tmp_path / "app", []))
    states = []
    client.engine_state.connect(states.append)
    with qtbot.waitSignal(client.ready, timeout=20000):
        client.start()
    try:
        for i in range(3):
            client.process.kill()
            qtbot.waitUntil(lambda: states.count("ready") == i + 2, timeout=20000)
        client.process.kill()
        qtbot.waitUntil(lambda: states[-1] == "failed", timeout=10000)
        assert states.count("restarting") == 3
    finally:
        client.shutdown()


_LAUNCHER = textwrap.dedent("""
    import multiprocessing as mp, os, sys, time
    def main():
        from fast_file_search.engine import protocol as P
        from fast_file_search.engine.service import engine_main
        root, app = sys.argv[1], sys.argv[2]
        boot = P.EngineBootstrap(P.EngineConfig(roots=(root,)), os.path.join(app, "cache"),
                                 os.path.join(app, "data"), os.path.join(app, "logs"))
        ctx = mp.get_context("spawn")
        a, b = ctx.Pipe()
        p = ctx.Process(target=engine_main, args=(b, boot), daemon=True)
        p.start()
        b.close()
        while not isinstance(a.recv(), P.Ready):
            pass
        print(p.pid, flush=True)
        time.sleep(120)
    if __name__ == "__main__":
        main()
""")


@slow
def test_engine_exits_when_parent_is_killed(core, tmp_path):
    root = _tree(tmp_path, 5)
    launcher = tmp_path / "launcher.py"
    launcher.write_text(_LAUNCHER)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), str(Path(__file__).parent),
                                         env.get("PYTHONPATH", "")])
    proc = subprocess.Popen([sys.executable, str(launcher), str(root), str(tmp_path / "app")],
                            stdout=subprocess.PIPE, text=True, env=env)
    try:
        line = proc.stdout.readline().strip()
        assert line.isdigit(), line
        child = int(line)
        assert pid_alive(child)
        proc.kill()
        proc.wait(10)
        t0 = time.monotonic()
        while pid_alive(child) and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        dt = time.monotonic() - t0
        assert not pid_alive(child), "engine still running 10 s after its parent died"
        assert dt <= 3.0, f"engine exited {dt:.2f} s after its parent was killed"
    finally:
        if proc.poll() is None:
            proc.kill()
