"""TEMPORARY diagnostic: repeat the in-process churn test with the writer trace on."""
import sys

import pytest

from test_index_vs_walk import _run


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS diagnostic")
@pytest.mark.parametrize("i", range(12))
def test_diag_churn_repeat(tmp_path, monkeypatch, i):
    monkeypatch.setenv("FFS_POWER", "ac")
    monkeypatch.setenv("FFS_DIAG", "1")
    _run(tmp_path, spawn=False)
