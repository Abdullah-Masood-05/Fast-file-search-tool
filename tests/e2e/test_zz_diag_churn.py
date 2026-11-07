"""TEMPORARY diagnostic: how often does the churn test hit the overlapping-diff cases
that crashed the writer before the fix (Engine._claim committing a dirty dir or demoting
a "new" task)? Fails on purpose with the counts so they show in the CI log."""
import collections
import sys

import pytest

from fast_file_search.engine import service as S
from test_index_vs_walk import _run

COUNTS = collections.Counter()


@pytest.fixture
def spy(monkeypatch):
    orig_claim = S.Engine._claim
    orig_listing = S.Engine._on_listing
    orig_resolve = S.Engine._resolve_conflicts
    orig_diff = S.Engine._diff
    state = {"mode": "batch"}
    last = {}

    def diff(self, task, entries, is_new, old=None, one_level=None):
        last[task.dir_id] = state["mode"] + ("/one" if old is not None else "")
        return orig_diff(self, task, entries, is_new, old, one_level)

    def on_listing(self, lst):
        state["mode"] = "L:" + lst.task.scan.mode + (":new" if lst.task.is_new else "")
        try:
            return orig_listing(self, lst)
        finally:
            state["mode"] = "batch"

    def claim(self, did, is_new=False):
        if did in self._dirty_dirs:
            COUNTS["dirty-commit " + state["mode"] + " after " + last.get(did, "?")] += 1
        if is_new and did not in self._new_dirs:
            COUNTS["demoted-new " + state["mode"] + " after " + last.get(did, "?")] += 1
        return orig_claim(self, did, is_new)

    def resolve(self, ops):
        COUNTS["conflict-resolved"] += 1
        return orig_resolve(self, ops)

    monkeypatch.setattr(S.Engine, "_claim", claim)
    monkeypatch.setattr(S.Engine, "_on_listing", on_listing)
    monkeypatch.setattr(S.Engine, "_resolve_conflicts", resolve)
    monkeypatch.setattr(S.Engine, "_diff", diff)
    return COUNTS


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS diagnostic")
@pytest.mark.parametrize("i", range(40))
def test_diag_churn_repeat(tmp_path, monkeypatch, spy, i):
    monkeypatch.setenv("FFS_POWER", "ac")
    before = sum(spy.values())
    _run(tmp_path, spawn=False)
    spy[f"runs-with-hits"] += 1 if sum(spy.values()) > before else 0


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS diagnostic")
def test_diag_zz_report():
    pytest.fail(f"DIAG {sys.platform}: {dict(COUNTS)}")
