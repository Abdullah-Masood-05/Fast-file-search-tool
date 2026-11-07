"""The in-process Engine end to end on temp trees (design C, D, A.10, G.4, G.7):
index == os.walk ground truth (Store and MemIndex), random mutations then an
incremental rescan, live watcher updates incl. REN+DIRPATH for in-root dir
renames, EPERM dirs keep their children, roots added/removed by Configure,
exclusion changes, rebuild, restart from the DB/snapshot, low power, user ops."""
import os
import random
import shutil
import sys
import time

import pytest

import fake_core
from fake_core import Harness, make_boot, select_core, walk_truth
from fast_file_search.engine import protocol as P
from fast_file_search.engine import service as S
from fast_file_search.engine import walker as walker_mod

INS, UPD, REN, DEL, DIR, DIRPATH = 1, 2, 3, 4, 5, 6


@pytest.fixture
def core(monkeypatch):
    kind = select_core(monkeypatch)
    monkeypatch.setenv("FFS_POWER", "ac")
    return kind


def build_tree(root, n_dirs=30, n_files=300, seed=1):
    rnd = random.Random(seed)
    dirs = [root]
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n_dirs):
        p = rnd.choice(dirs) / rnd.choice(["src", "lib", "docs", "Café", "日本", "pkg", "node_modules", "a b"]) \
            if i % 7 else root / f"top{i}"
        p = p.with_name(p.name + str(i))
        p.mkdir(parents=True, exist_ok=True)
        dirs.append(p)
    nm = root / "proj" / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("x")
    for i in range(n_files):
        d = rnd.choice(dirs)
        (d / f"{rnd.choice(['index', 'README', 'GiftTopIcon', 'résumé', 'main'])}{i}"
              f"{rnd.choice(['.js', '.md', '.png', '.txt', ''])}").write_bytes(b"x" * rnd.randint(0, 50))
    return dirs


def start(tmp_path, roots, **cfg):
    h = Harness(make_boot(tmp_path / "app", roots, **cfg))
    r = h.start()
    assert isinstance(r, P.Ready), r
    return h, r


def test_initial_index_matches_walk(core, tmp_path):
    root = tmp_path / "tree"
    build_tree(root)
    h, ready = start(tmp_path, [root])
    try:
        assert ready.protocol_version == P.PROTOCOL_VERSION
        assert any(c.startswith("qt_modules:") for c in ready.capabilities)
        h.settle()
        assert h.index_paths() == walk_truth(root)
        st = h.wait(lambda m: isinstance(m, P.IndexStatus) and m.state == "idle" and m.total_entries > 0)
        assert st.roots[0].path == str(root) and st.roots[0].state == "ok"
        assert st.total_entries == len(walk_truth(root))
        res = h.search("readme")
        assert res.total == sum(1 for p in walk_truth(root) if "readme" in os.path.basename(p).lower())
        assert not res.partial
        assert all(r.path.startswith(str(root)) for r in res.rows)
    finally:
        h.stop()


def mutate(root, rnd, steps=60):
    for _ in range(steps):
        all_files = [os.path.join(dp, f) for dp, dn, fn in os.walk(root) for f in fn if "node_modules" not in dp]
        all_dirs = [os.path.join(dp, d) for dp, dn, fn in os.walk(root) for d in dn if "node_modules" not in dp
                    and d != "node_modules"]
        op = rnd.random()
        try:
            if op < 0.25 or not all_files:
                d = rnd.choice(all_dirs or [str(root)])
                with open(os.path.join(d, f"new{rnd.randint(0, 10 ** 6)}.txt"), "w") as fh:
                    fh.write("y" * rnd.randint(0, 9))
            elif op < 0.45:
                os.remove(rnd.choice(all_files))
            elif op < 0.6:
                f = rnd.choice(all_files)
                with open(f, "ab") as fh:
                    fh.write(b"more")
                os.utime(f, (time.time() + rnd.randint(10, 999), time.time() + rnd.randint(10, 999)))
            elif op < 0.7 and all_dirs:
                d = rnd.choice(all_dirs)
                os.rename(d, os.path.join(os.path.dirname(d), "ren" + os.path.basename(d)))
            elif op < 0.8 and all_dirs:
                shutil.rmtree(rnd.choice(all_dirs))
            elif op < 0.9:
                d = os.path.join(rnd.choice(all_dirs or [str(root)]), f"newdir{rnd.randint(0, 10 ** 6)}")
                os.makedirs(os.path.join(d, "inner"))
                open(os.path.join(d, "inner", "deep.txt"), "w").close()
            else:
                f = rnd.choice(all_files)
                os.rename(f, os.path.join(rnd.choice(all_dirs or [str(root)]), "mv" + os.path.basename(f)))
        except OSError:
            pass


@pytest.mark.parametrize("seed", [3, 11])
def test_incremental_rescan_after_random_mutations(core, tmp_path, seed):
    root = tmp_path / "tree"
    build_tree(root, seed=seed)
    h, _ = start(tmp_path, [root], live_monitoring=False)
    try:
        h.settle()
        mutate(root, random.Random(seed))
        h.engine.handle(P.IndexCommand("update"))
        time.sleep(0.1)
        h.settle()
        assert h.index_paths() == walk_truth(root)
        # sizes/mtimes are current too
        ents = h.entries()
        for p in walk_truth(root):
            st = os.lstat(p)
            if not os.path.isdir(p):
                assert ents[p][1] == st.st_size and ents[p][2] == int(st.st_mtime), p
    finally:
        h.stop()


def test_live_updates_match_walk(core, tmp_path):
    root = tmp_path / "tree"
    build_tree(root, seed=5)
    h, _ = start(tmp_path, [root])
    try:
        h.settle()
        rnd = random.Random(5)
        for _ in range(3):
            mutate(root, rnd, steps=25)
            time.sleep(0.3)
        time.sleep(0.8)
        h.settle(timeout=30)
        assert h.index_paths() == walk_truth(root)
    finally:
        h.stop()


def test_dir_rename_is_ren_plus_dirpath(core, tmp_path):
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    for i in range(20):
        (root / "a" / "b" / f"f{i}.txt").write_text("x")
    h, _ = start(tmp_path, [root])
    try:
        h.settle()
        before = h.entries()
        log = h.record_ops()
        os.rename(root / "a" / "b", root / "a" / "c")
        h.wait(lambda m: isinstance(m, P.IndexChanged) and m.index_generation > h.engine._gen - 1, 5)
        time.sleep(0.8)
        h.settle()
        flat = [op for batch in log for op in batch]
        kinds = [op[0] for op in flat]
        assert REN in kinds and DIRPATH in kinds
        assert INS not in kinds and DEL not in kinds                # not delete + re-add
        ren = next(op for op in flat if op[0] == REN)
        assert ren[3] == "c"
        dp = next(op for op in flat if op[0] == DIRPATH)
        assert dp[2] == str(root / "a" / "c")
        assert h.index_paths() == walk_truth(root)
        res = h.search("f1")
        assert res.total >= 1 and all(os.sep + "c" + os.sep in r.path for r in res.rows)
        assert len(h.entries()) == len(before)
    finally:
        h.stop()


def test_burst_of_creates_is_searchable(core, tmp_path):
    root = tmp_path / "tree"
    (root / "burst").mkdir(parents=True)
    h, _ = start(tmp_path, [root])
    try:
        h.settle()
        n = 2000
        for i in range(n):
            open(root / "burst" / f"burstfile{i}.dat", "w").close()
        t_end = time.monotonic()
        while True:
            res = h.search("burstfile")
            if res.total == n or time.monotonic() - t_end > 5:
                break
            time.sleep(0.1)
        lat = time.monotonic() - t_end
        assert res.total == n
        assert lat <= 3.0, lat
    finally:
        h.stop()


def test_excluded_flood_produces_no_ops(core, tmp_path):
    root = tmp_path / "tree"
    nm = root / "proj" / "node_modules"
    nm.mkdir(parents=True)
    h, _ = start(tmp_path, [root])
    try:
        h.settle()
        log = h.record_ops()
        for i in range(2000):
            (nm / f"f{i}.js").write_text("x")
        time.sleep(1.5)
        h.settle()
        assert [op for b in log for op in b] == []
    finally:
        h.stop()


def test_unreadable_dir_keeps_children(core, tmp_path, monkeypatch):
    root = tmp_path / "tree"
    (root / "locked" / "inner").mkdir(parents=True)
    (root / "locked" / "keep.txt").write_text("x")
    (root / "locked" / "inner" / "deep.txt").write_text("x")
    h, _ = start(tmp_path, [root], live_monitoring=False)
    try:
        h.settle()
        assert str(root / "locked" / "keep.txt") in h.index_paths()
        locked = str(root / "locked")
        real = os.scandir

        def scandir(path):
            if os.fspath(path) == locked:
                raise PermissionError(13, "Permission denied", locked)
            return real(path)
        monkeypatch.setattr(walker_mod.WalkContext, "__init__", _ctx_init_with(scandir))
        h.engine.handle(P.IndexCommand("rescan"))
        time.sleep(0.1)
        h.settle()
        paths = h.index_paths()
        assert str(root / "locked" / "keep.txt") in paths and str(root / "locked" / "inner" / "deep.txt") in paths
        assert h.entries()[locked][0] & P.UNREADABLE
        notices = h.of(P.Notice)
        assert any(n.code == "denied_dirs" for n in notices)
        monkeypatch.undo()
        monkeypatch.setenv("FFS_POWER", "ac")
        h.engine.handle(P.IndexCommand("rescan"))
        time.sleep(0.1)
        h.settle()
        assert not h.entries()[locked][0] & P.UNREADABLE                # cleared when readable again
    finally:
        h.stop()


def _ctx_init_with(scandir):
    orig = walker_mod.WalkContext.__init__

    def init(self, ex, *, devices=None, scandir_=None, root="", **kw):
        orig(self, ex, devices=devices, scandir=scandir, root=root)
    return init


def test_denied_and_missing_roots(core, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    missing = tmp_path / "missing"
    h, _ = start(tmp_path, [root, missing], live_monitoring=False)
    try:
        h.settle()
        n = h.wait(lambda m: isinstance(m, P.Notice) and m.code == "root_missing")
        assert dict(n.data)["path"] == str(missing)
        st = h.wait(lambda m: isinstance(m, P.IndexStatus) and len(m.roots) == 2 and
                    {r.state for r in m.roots} == {"ok", "missing"})
        assert st.state == "idle"
    finally:
        h.stop()


def test_configure_adds_and_removes_roots_and_exclusions(core, tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    for r in (r1, r2):
        (r / "build").mkdir(parents=True)
        (r / "build" / "out.o").write_text("x")
        (r / "keep.txt").write_text("x")
    h, _ = start(tmp_path, [r1])
    try:
        h.settle()
        assert h.index_paths() == walk_truth(r1)
        h.engine.handle(P.Configure(P.EngineConfig(roots=(str(r1), str(r2)))))
        time.sleep(0.2)
        h.settle()
        assert h.index_paths() == walk_truth(r1) | walk_truth(r2)
        mark = len(h.msgs)
        h.engine.handle(P.Configure(P.EngineConfig(roots=(str(r2),), exclude_patterns=("build",))))
        time.sleep(0.2)
        h.settle()
        assert h.index_paths() == {str(r2 / "keep.txt")}
        st = h.wait(lambda m: isinstance(m, P.IndexStatus) and len(m.roots) == 1 and m.state == "idle", 10, mark)
        assert st.roots[0].path == str(r2)
        # nested / duplicate roots are deduped
        h.engine.handle(P.Configure(P.EngineConfig(roots=(str(r2), str(r2 / "build"), str(r2)))))
        time.sleep(0.2)
        h.settle()
        assert len(h.call(lambda: h.engine.store.roots())) == 1
    finally:
        h.stop()


def test_rebuild(core, tmp_path):
    root = tmp_path / "tree"
    build_tree(root, n_dirs=5, n_files=40)
    h, _ = start(tmp_path, [root], live_monitoring=False)
    try:
        h.settle()
        h.engine.handle(P.IndexCommand("rebuild"))
        time.sleep(0.2)
        h.settle()
        assert h.index_paths() == walk_truth(root)
    finally:
        h.stop()


def test_restart_uses_snapshot_and_catches_up(core, tmp_path):
    root = tmp_path / "tree"
    build_tree(root, n_dirs=8, n_files=80)
    boot = make_boot(tmp_path / "app", [root])
    h = Harness(boot)
    assert isinstance(h.start(), P.Ready)
    h.settle()
    h.stop()
    (root / "added_while_closed.txt").write_text("x")
    victim = next(p for p in sorted(walk_truth(root)) if p.endswith(".txt") and "node_modules" not in p)
    os.remove(victim)
    h2 = Harness(boot)
    ready = h2.start()
    try:
        assert "snapshot" in ready.capabilities
        assert ready.status.total_entries > 0                       # searchable before any rescan
        h2.settle()
        assert h2.index_paths() == walk_truth(root)
    finally:
        h2.stop()


def test_search_flow_pages_refresh_and_latest_wins(core, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(30):
        (root / f"item{i:02d}.txt").write_text("x")
    h, _ = start(tmp_path, [root])
    try:
        h.settle()
        res = h.search("item", limit=10)
        assert res.total == 30 and len(res.rows) == 10 and res.offset == 0
        h.engine.handle(P.FetchPage(res.gen, 10, 10))
        page = h.wait(lambda m: isinstance(m, P.SearchResult) and m.gen == res.gen and m.offset == 10)
        assert len(page.rows) == 10 and not {r.id for r in page.rows} & {r.id for r in res.rows}
        # live refresh: a new matching file arrives -> refresh=True for the same gen
        (root / "item99.txt").write_text("x")
        ref = h.wait(lambda m: isinstance(m, P.SearchResult) and m.gen == res.gen and m.refresh, 6)
        assert ref.total == 31
        # latest wins: a burst of requests yields the last one
        mark = len(h.msgs)
        for g in range(100, 110):
            h.engine.handle(P.SearchRequest(g, "item0"))
        last = h.wait(lambda m: isinstance(m, P.SearchResult) and m.gen == 109, 5, mark)
        assert last.total == 10
        time.sleep(0.2)
        gens = [m.gen for m in h.msgs[mark:] if isinstance(m, P.SearchResult)]
        assert gens[-1] == 109 and gens == sorted(gens)
    finally:
        h.stop()


def test_user_ops_and_item_info(core, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    f = root / "fav.txt"
    f.write_text("x")
    h, _ = start(tmp_path, [root], live_monitoring=False)
    try:
        h.settle()
        h.engine.handle(P.UserOp("favorite", (str(f),)))
        h.engine.handle(P.UserOp("set_tags", (str(f),), ["work", "q3"]))
        h.engine.handle(P.UserOp("set_note", (str(f),), "remember"))
        h.engine.handle(P.ItemInfoRequest(7, str(f)))
        info = h.wait(lambda m: isinstance(m, P.ItemInfo) and m.req_id == 7)
        assert info.favorite and set(info.tags) == {"work", "q3"} and info.note == "remember"
        res = h.search("fav")
        assert res.rows and res.rows[0].favorite
        h.search("fav", commit_history=True)
        h.engine.handle(P.UserOp("unfavorite", (str(f),)))
        h.engine.handle(P.ItemInfoRequest(8, str(f)))
        assert not h.wait(lambda m: isinstance(m, P.ItemInfo) and m.req_id == 8).favorite
    finally:
        h.stop()


def test_low_power_structure_pass_and_throttle(core, tmp_path, monkeypatch):
    root = tmp_path / "tree"
    build_tree(root, n_dirs=6, n_files=30)
    boot = make_boot(tmp_path / "app", [root], live_monitoring=False)
    h = Harness(boot)
    h.start()
    h.settle()
    h.stop()
    # change a file in place (dir mtime unchanged) and add one (dir mtime changes)
    victim = next(p for p in sorted(walk_truth(root)) if os.path.isfile(p) and "node_modules" not in p)
    with open(victim, "ab") as fh:
        fh.write(b"grown")
    os.utime(victim, (time.time() + 500, time.time() + 500))
    (root / "fresh.txt").write_text("x")
    monkeypatch.setenv("FFS_POWER", "battery")
    h2 = Harness(boot)
    h2.start()
    try:
        h2.settle()
        e = h2.engine
        assert e.low_power and e.pool.threads == 1 and e.pool.low_power
        assert str(root / "fresh.txt") in h2.index_paths()             # structure pass finds adds
        st = h2.wait(lambda m: isinstance(m, P.IndexStatus) and m.state == "paused")
        assert st.message == "Index paused (on battery)"
        # back on AC: the deferred full pass runs and picks up the in-place change
        e._wq.put(("power", fake_core_power(False)))
        time.sleep(0.2)
        h2.settle()
        assert h2.entries()[victim][1] == os.path.getsize(victim)
        assert not e.low_power and e.pool.threads == S.default_walker_threads()
    finally:
        h2.stop()


def fake_core_power(on_battery):
    from fast_file_search.engine.power import PowerState
    return PowerState(on_battery, False)


def test_own_files_never_indexed_when_cache_is_inside_a_root(core, tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    (root / "doc.txt").write_text("x")
    h = Harness(make_boot(root / "app", [root]))
    assert isinstance(h.start(), P.Ready)
    try:
        h.settle()
        log = h.record_ops()
        for i in range(5):
            h.search(f"doc{i}", commit_history=True)
        time.sleep(1.5)
        h.settle()
        paths = h.index_paths()
        assert not any(p.startswith(str(root / "app") + os.sep) for p in paths), paths
        assert [op for b in log for op in b] == []
    finally:
        h.stop()


def test_corrupt_db_is_rebuilt(core, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("x")
    boot = make_boot(tmp_path / "app", [root], live_monitoring=False)
    db = os.path.join(boot.cache_dir, S.DB_NAME)
    with open(db, "wb") as fh:
        fh.write(b"this is not a database" * 500)
    h = Harness(boot)
    assert isinstance(h.start(), P.Ready)
    try:
        h.wait(lambda m: isinstance(m, P.Notice) and m.code == "index_rebuilt")
        h.settle()
        assert h.index_paths() == walk_truth(root)
    finally:
        h.stop()


def test_engine_lock_blocks_a_second_engine(core, tmp_path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    boot = make_boot(tmp_path / "app", [root], live_monitoring=False)
    h = Harness(boot)
    assert isinstance(h.start(), P.Ready)
    try:
        lock = S._FileLock(S.Path(boot.cache_dir) / S.LOCK_NAME)
        assert not lock.acquire(timeout=0.3)
    finally:
        h.stop()
    lock = S._FileLock(S.Path(boot.cache_dir) / S.LOCK_NAME)
    assert lock.acquire(timeout=1.0)
    lock.release()


def test_hello_version_mismatch_is_fatal(core, tmp_path):
    h, _ = start(tmp_path, [tmp_path], live_monitoring=False)
    try:
        h.engine.handle(P.Hello(protocol_version=999))
        f = h.wait(lambda m: isinstance(m, P.Fatal))
        assert "Protocol mismatch" in f.text
        assert h.engine.dead.wait(5)
    finally:
        h.stop()


# ----------------------------------------------------------------------------- permission wait, power

def test_protected_folder_names():
    home = "/Users/me"
    assert S.protected_folder_name("/Users/me/Documents/x", home, "darwin") == "Documents"
    assert S.protected_folder_name("/Users/me/Desktop", home, "darwin") == "Desktop"
    assert S.protected_folder_name("/Users/me/Library/Mobile Documents/a", home, "darwin") == "iCloud Drive"
    assert S.protected_folder_name("/Volumes/USB/x", home, "darwin") == "USB"
    assert S.protected_folder_name("/Users/me/Code", home, "darwin") == ""
    assert S.protected_folder_name(r"C:\Users\me\Documents", r"C:\Users\me", "win32") == ""


def test_slow_listing_reports_waiting(core, tmp_path, monkeypatch):
    import threading
    root = tmp_path / "tree"
    (root / "slow").mkdir(parents=True)
    gate = threading.Event()
    slow = str(root / "slow")
    real = os.scandir

    def scandir(path):
        if os.fspath(path) == slow:
            gate.wait(10)
        return real(path)
    monkeypatch.setattr(walker_mod.WalkContext, "__init__", _ctx_init_with(scandir))
    h, _ = start(tmp_path, [root], live_monitoring=False)
    try:
        st = h.wait(lambda m: isinstance(m, P.IndexStatus) and m.message.startswith("Waiting for"), 8)
        assert st.state == "scanning" and "slow" in st.message
        gate.set()
        h.settle()
        assert h.wait(lambda m: isinstance(m, P.IndexStatus) and m.state == "idle" and not m.message, 5)
    finally:
        gate.set()
        h.stop()


def test_power_probes(tmp_path, monkeypatch):
    from fast_file_search.engine import power
    assert power.parse_pmset("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t80%; discharging")
    assert not power.parse_pmset("Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged")
    assert not power.parse_pmset("")
    base = tmp_path / "ps"
    for name, kind, online in (("AC", "Mains", "0"), ("BAT0", "Battery", "1")):
        (base / name).mkdir(parents=True)
        (base / name / "type").write_text(kind + "\n")
        (base / name / "online").write_text(online + "\n")
    assert power.linux_on_battery(str(base))
    (base / "AC" / "online").write_text("1\n")
    assert not power.linux_on_battery(str(base))
    assert not power.linux_on_battery(str(tmp_path / "none"))         # desktops without supplies: AC
    monkeypatch.setenv("FFS_POWER", "battery")
    assert power.power_state().low_power
    monkeypatch.setenv("FFS_POWER", "saver")
    assert power.power_state().saver
    monkeypatch.delenv("FFS_POWER")
    assert isinstance(power.power_state(), power.PowerState)          # the real probe never raises
    states = iter([power.AC, power.PowerState(True, False)])
    seen = []
    mon = power.PowerMonitor(seen.append, interval=0.05, probe=lambda: next(states, power.PowerState(True, False)))
    try:
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen == [power.PowerState(True, False)]
    finally:
        mon.stop()


def test_macos_bridge_is_inert_elsewhere():
    from fast_file_search.engine import macos
    if sys.platform != "darwin":
        assert macos.begin_activity() is None and macos.low_power_mode() is False
        a = macos.ActivityAssertion()
        a.set_active(True)
        a.set_active(False)


def _stale_tree(root):
    (root / "a" / "lib4" / "sub").mkdir(parents=True)
    (root / "a" / "lib4" / "keep.txt").write_text("k")
    (root / "a" / "lib4" / "sub" / "deep.txt").write_text("d")
    (root / "other").mkdir()
    (root / "other" / "x.txt").write_text("x")


def test_events_under_old_dir_path_in_an_earlier_batch_than_the_dir_move(core, tmp_path):
    """Regression (Linux CI churn): events under a folder's old path were applied in a
    batch *before* the batch with that folder's move, but after the move had happened on
    disk. They found nothing at the old path (a create inserted nothing, a modify deleted
    the file, a move into the folder deleted the source) and the later REN+DIRPATH moved
    the stale subtree. The engine must reconcile the moved folder."""
    from fast_file_search.engine.watcher import FILE, Batch
    root = tmp_path / "tree"
    _stale_tree(root)
    h, _ = start(tmp_path, [root], live_monitoring=False)
    try:
        h.settle()
        old, new = root / "a" / "lib4", root / "moved_dir_lib4"
        (old / "created.txt").write_text("c")                       # create under the old path
        (old / "sub" / "created2.txt").write_text("c")
        with open(old / "sub" / "deep.txt", "ab") as fh:             # modify under the old path
            fh.write(b"more")
        os.rename(root / "other" / "x.txt", old / "moved_x.txt")     # move into the folder
        os.rename(old, new)                                         # ... and only then the folder moves
        b1 = Batch({str(old / "created.txt"): FILE, str(old / "sub" / "created2.txt"): FILE,
                    str(old / "sub" / "deep.txt"): FILE},
                   [(str(root / "other" / "x.txt"), str(old / "moved_x.txt"), False)], False)
        b2 = Batch({}, [(str(old), str(new), True)], False)
        h.call(lambda: h.engine._apply_batch(b1))
        h.call(lambda: h.engine._apply_batch(b2))
        h.settle()
        assert h.index_paths() == walk_truth(root)
        ents = h.entries()
        p = str(new / "sub" / "deep.txt")
        assert ents[p][1] == os.lstat(p).st_size
    finally:
        h.stop()


def test_dir_move_after_recent_events_rewalks_without_churn(core, tmp_path):
    """A folder that had events applied shortly before it moved is re-walked after
    its REN+DIRPATH (see the test above); when nothing was stale the walk adds no ops
    and the move stays a rename, not delete + re-add. A folder without recent events is
    not re-walked."""
    from fast_file_search.engine.watcher import FILE, Batch
    root = tmp_path / "tree"
    _stale_tree(root)
    (root / "quiet" / "inner").mkdir(parents=True)
    (root / "quiet" / "inner" / "q.txt").write_text("q")
    h, _ = start(tmp_path, [root], live_monitoring=False)
    try:
        h.settle()
        old, new = root / "a" / "lib4", root / "a" / "lib5"
        (old / "fresh.txt").write_text("f")
        h.call(lambda: h.engine._apply_batch(Batch({str(old / "fresh.txt"): FILE}, [], False)))
        h.settle()
        log = h.record_ops()
        listings = h.engine.stats.listings
        os.rename(old, new)
        h.call(lambda: h.engine._apply_batch(Batch({}, [(str(old), str(new), True)], False)))
        h.settle()
        kinds = [op[0] for batch in log for op in batch]
        assert REN in kinds and DIRPATH in kinds
        assert INS not in kinds and DEL not in kinds
        assert h.engine.stats.listings >= listings + 2            # lib5 and lib5/sub were re-walked
        assert h.index_paths() == walk_truth(root)
        listings = h.engine.stats.listings
        os.rename(root / "quiet", root / "quiet2")
        h.call(lambda: h.engine._apply_batch(
            Batch({}, [(str(root / "quiet"), str(root / "quiet2"), True)], False)))
        h.settle()
        assert h.engine.stats.listings == listings                # no recent events there: no walk
        assert h.index_paths() == walk_truth(root)
    finally:
        h.stop()


def test_outermost_drops_nested_paths():
    j = os.path.join
    a = j(os.sep + "r", "a")
    got = S._outermost([j(a, "c"), a + " b", a, j(a, "c", "d"), a])
    assert sorted(got) == sorted([a, a + " b"])
