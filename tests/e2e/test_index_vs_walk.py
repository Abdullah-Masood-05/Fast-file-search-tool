"""Ground truth: the engine's entries must equal ``os.walk`` (default excludes applied).

A deterministic tree is generated, the real engine (in process, and through the real spawn
path) indexes it, then the tree is mutated with the watcher on (create, modify, rename,
move, delete, whole folders, a burst, churn inside excluded folders). After the engine has
settled the listing read back through ``SearchRequest`` must equal the walk: no missing
entries, no ghosts, no stale sizes.
"""
import os
import random
import shutil
import time
from pathlib import Path

import pytest

from e2e_harness import EngineHandle, make_boot, walk_truth

STEMS = ["index", "README", "GiftTopIcon", "résumé", "main", "report", "Café", "日本語", "über", "data"]
EXTS = [".js", ".md", ".png", ".txt", ".py", ".json", ""]
DIR_NAMES = ["src", "lib", "docs", "Café", "日本", "pkg", "assets", "a b", "tests"]


def build_tree(root: Path, seed: int = 20, n_dirs: int = 40, n_files: int = 700) -> list:
    rnd = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    dirs = [root]
    for i in range(n_dirs):
        parent = rnd.choice(dirs)
        d = parent / f"{rnd.choice(DIR_NAMES)}{i}"
        d.mkdir()
        dirs.append(d)
    # excluded folders that must never show up
    for name in ("node_modules", ".git", "__pycache__"):
        d = rnd.choice(dirs) / name
        d.mkdir(exist_ok=True)
        (d / "hidden_by_exclude.js").write_text("x")
    for i in range(n_files):
        d = rnd.choice(dirs)
        (d / f"{rnd.choice(STEMS)}{i}{rnd.choice(EXTS)}").write_bytes(b"x" * rnd.randint(0, 64))
    return dirs


def mutate(root: Path, seed: int = 21) -> None:
    """Deterministic churn (the watcher is on). Each phase is separated by a short pause so
    both coalesced and spread-out events are exercised."""
    rnd = random.Random(seed)

    def files():
        return sorted(p for p in root.rglob("*") if p.is_file()
                      and not {"node_modules", ".git", "__pycache__"} & set(p.relative_to(root).parts))

    def dirs():
        return sorted(p for p in root.rglob("*") if p.is_dir()
                      and not {"node_modules", ".git", "__pycache__"} & set(p.relative_to(root).parts))

    # creates: files, a new folder with a nested tree
    for i in range(30):
        d = rnd.choice(dirs())
        (d / f"created{i}.txt").write_text("y" * i)
    new = root / "brand_new" / "deeper" / "deepest"
    new.mkdir(parents=True)
    for i in range(10):
        (new / f"leaf{i}.md").write_text("z")
    time.sleep(0.4)

    # modifies: grow files and push their mtimes
    for f in rnd.sample(files(), 25):
        with open(f, "ab") as fh:
            fh.write(b"appended" * 5)
        t = time.time() + rnd.randint(10, 900)
        os.utime(f, (t, t))
    time.sleep(0.4)

    # renames: files, then a folder that has children
    for f in rnd.sample(files(), 15):
        f.rename(f.with_name("renamed_" + f.name))
    victim = rnd.choice([d for d in dirs() if any(d.iterdir()) and d != root / "brand_new"])
    victim.rename(victim.with_name("folder_renamed_" + victim.name))
    time.sleep(0.4)

    # moves across folders
    all_dirs = dirs()
    for f in rnd.sample(files(), 15):
        shutil.move(str(f), str(rnd.choice(all_dirs) / ("moved_" + f.name)))
    sub = rnd.choice([d for d in dirs() if d.parent != root and any(d.iterdir())])
    shutil.move(str(sub), str(root / ("moved_dir_" + sub.name)))
    time.sleep(0.4)

    # deletes: files and a whole subtree
    for f in rnd.sample(files(), 25):
        f.unlink()
    shutil.rmtree(rnd.choice([d for d in dirs() if d.parent != root and any(d.iterdir())]))
    time.sleep(0.4)

    # churn inside excluded folders and a burst
    nm = root / "proj" / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    for i in range(80):
        (nm / f"f{i}.js").write_text("x")
    (root / "proj" / "__pycache__").mkdir()
    (root / "proj" / "__pycache__" / "m.pyc").write_bytes(b"x")
    burst = root / "burst"
    burst.mkdir()
    for i in range(400):
        (burst / f"burst{i}.dat").write_text("b" * (i % 7))
    for i in range(0, 400, 4):
        (burst / f"burst{i}.dat").unlink()


@pytest.fixture(autouse=True)
def _ac_power(monkeypatch):
    monkeypatch.setenv("FFS_POWER", "ac")          # never throttle the scan on a battery


def _run(tmp_path, spawn: bool):
    root = tmp_path / "tree"
    build_tree(root)
    eng = EngineHandle(make_boot(tmp_path / "app", [root], live_monitoring=True), spawn=spawn)
    try:
        eng.wait_ready()
        eng.wait_first_scan()
        ok, diff = eng.converge(root, timeout=60)
        assert ok, f"initial index != os.walk: {diff}"
        initial = walk_truth(root)
        assert not any(("node_modules" in p or "__pycache__" in p or os.sep + ".git" + os.sep in p)
                       for p in initial)

        mutate(root)
        ok, diff = eng.converge(root, timeout=60)
        assert ok, f"index != os.walk after mutations (watcher on): {diff}"

        # the search path agrees with the listing: a name query finds exactly the walk's matches
        truth = walk_truth(root)
        eng.gen += 1
        g = eng.gen
        with eng.cv:
            mark = len(eng.msgs)
        from fast_file_search.engine import protocol as P
        eng.chan.send(P.SearchRequest(g, "burst", limit=1000, suggestions=False, root_counts=False))
        res = eng.wait(lambda m: isinstance(m, P.SearchResult) and m.gen == g and not m.refresh, 30, mark)
        want = sum(1 for p in truth if "burst" in os.path.basename(p).lower())
        assert res.total == want, (res.total, want)
    finally:
        code = eng.stop()
    return code


def test_engine_in_process_matches_walk_under_churn(tmp_path):
    _run(tmp_path, spawn=False)


@pytest.mark.slow
def test_engine_spawned_matches_walk_under_churn(tmp_path):
    assert _run(tmp_path, spawn=True) in (0, None)


@pytest.mark.slow
def test_rescan_after_offline_changes_matches_walk(tmp_path):
    """Changes made while the engine is not running are picked up by the next start (DB
    and snapshot reuse plus the incremental scan) and still equal the walk."""
    root = tmp_path / "tree"
    build_tree(root, seed=33, n_files=300)
    boot = make_boot(tmp_path / "app", [root])
    eng = EngineHandle(boot, spawn=True)
    try:
        eng.wait_ready()
        eng.wait_first_scan()
        assert eng.converge(root, timeout=60)[0]
    finally:
        eng.stop()
    mutate(root, seed=34)
    eng = EngineHandle(boot, spawn=True)
    try:
        eng.wait_ready()
        ok, diff = eng.converge(root, timeout=60)
        assert ok, f"restart index != os.walk: {diff}"
    finally:
        eng.stop()
