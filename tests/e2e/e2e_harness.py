"""Shared helpers for the end-to-end tests: a message-level engine channel (in-process or
through the real ``multiprocessing`` spawn path), an ``os.walk`` ground truth that follows
the engine's default exclusions, and an Entry listing read back through ``SearchRequest``.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from fast_file_search.config import DEFAULT_EXCLUDES
from fast_file_search.engine import protocol as P
from fast_file_search.engine.exclusions import COMMON_NAMES
from fast_file_search.selftest import _Channel

EVERYTHING = P.FilterState(kinds=frozenset({"file", "folder"}))
_FOLD = os.name == "nt" or sys.platform == "darwin"


def make_boot(tmp: Path, roots, **cfg) -> P.EngineBootstrap:
    for d in ("cache", "data", "logs"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    return P.EngineBootstrap(P.EngineConfig(roots=tuple(str(r) for r in roots), **cfg),
                             str(tmp / "cache"), str(tmp / "data"), str(tmp / "logs"))


def walk_truth(root) -> dict:
    """path -> (is_dir, size) for everything below root, minus the default excludes
    (names match case-insensitively on Windows and macOS). Symlinks are not followed."""
    skip = set(COMMON_NAMES) | set(DEFAULT_EXCLUDES)
    if _FOLD:
        skip = {s.casefold() for s in skip}

    def excluded(name):
        return (name.casefold() if _FOLD else name) in skip

    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        keep = []
        for d in dirnames:
            if excluded(d):
                continue
            full = os.path.join(dirpath, d)
            out[full] = (True, 0)
            if not os.path.islink(full):
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if excluded(f):
                continue
            full = os.path.join(dirpath, f)
            try:
                size = os.lstat(full).st_size
            except OSError:
                continue
            out[full] = (False, size)
    return out


class EngineHandle:
    """A started engine with a background reader. ``spawn=True`` uses a real child process."""

    def __init__(self, boot: P.EngineBootstrap, spawn: bool):
        self.chan = _Channel(boot, in_process=not spawn)
        self.msgs: list = []
        self.cv = threading.Condition()
        self.alive = True
        self.gen = 0
        self.chan.start()
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        while self.alive:
            try:
                m = self.chan.recv(0.1)
            except (EOFError, OSError):
                break
            if m is None:
                continue
            with self.cv:
                self.msgs.append(m)
                self.cv.notify_all()

    def wait(self, pred, timeout=60.0, start=0):
        end = time.monotonic() + timeout
        with self.cv:
            while True:
                for m in self.msgs[start:]:
                    if type(m).__name__ == "Fatal":
                        raise AssertionError(f"engine fatal: {m.text}\n{m.traceback}")
                    if pred(m):
                        return m
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timeout; last messages {[type(m).__name__ for m in self.msgs[-12:]]}")
                self.cv.wait(min(left, 0.1))

    def wait_ready(self, timeout=60.0):
        return self.wait(lambda m: isinstance(m, P.Ready), timeout)

    def wait_first_scan(self, timeout=120.0):
        def done(m):
            return (isinstance(m, P.IndexStatus) and m.state == "idle" and bool(m.roots)
                    and all(r.state == "ok" for r in m.roots))
        return self.wait(done, timeout)

    def listing(self, timeout=30.0) -> dict:
        """path -> (is_dir, size) for every entry, through a real search request."""
        self.gen += 1
        g = self.gen
        with self.cv:
            mark = len(self.msgs)
        self.chan.send(P.SearchRequest(g, "", EVERYTHING, limit=2_000_000, suggestions=False, root_counts=False))
        res = self.wait(lambda m: isinstance(m, P.SearchResult) and m.gen == g and not m.refresh, timeout, mark)
        assert res.error is None, res.error
        rows = list(res.rows)
        if len(rows) < res.total:       # engine caps a page: fetch the rest
            self.chan.send(P.FetchPage(g, len(rows), res.total - len(rows)))
            more = self.wait(lambda m: isinstance(m, P.SearchResult) and m.gen == g and m.offset == len(rows),
                             timeout, mark)
            rows += list(more.rows)
        assert len(rows) == res.total, (len(rows), res.total)
        return {r.path: (bool(r.flags & P.FOLDER), 0 if r.flags & P.FOLDER else r.size) for r in rows}

    def converge(self, root, timeout=45.0, stable_for=1.0):
        """Poll until the engine listing equals the os.walk truth and stays equal for
        ``stable_for`` seconds. Returns (ok, diff) where diff describes the last mismatch."""
        end = time.monotonic() + timeout
        equal_since = None
        diff = None
        while time.monotonic() < end:
            truth = walk_truth(root)
            got = self.listing()
            if got == truth:
                equal_since = equal_since or time.monotonic()
                if time.monotonic() - equal_since >= stable_for:
                    return True, None
            else:
                equal_since = None
                diff = describe_diff(truth, got)
            time.sleep(0.25)
        return False, diff

    def stop(self):
        self.alive = False
        try:
            return self.chan.stop()
        finally:
            self._t.join(2)


def describe_diff(truth: dict, got: dict, limit=8) -> dict:
    missing = sorted(set(truth) - set(got))
    extra = sorted(set(got) - set(truth))
    changed = sorted(p for p in set(truth) & set(got) if truth[p] != got[p])
    return {"missing": (len(missing), missing[:limit]), "extra": (len(extra), extra[:limit]),
            "changed": (len(changed), [(p, truth[p], got[p]) for p in changed[:limit]])}
