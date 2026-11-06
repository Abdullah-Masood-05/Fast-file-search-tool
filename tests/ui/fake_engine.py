"""FakeEngineClient: the GUI-side ``EngineClient`` API (design H.2) without a process.

It has the same signals and methods, records every call, and answers searches from
an in-memory ``FakeDataset`` (a tiny brute-force evaluator of text + FilterState),
always asynchronously through the event loop like the real client. ``delay_ms`` lets
tests deliver replies late (stale-gen tests); ``auto=False`` holds replies until
``deliver()``.

Also provides the shared fixtures for tests/ui (import them into a test module with
``from fake_engine import *``).
"""
from __future__ import annotations

import fnmatch
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pytest
from PySide6.QtCore import QObject, QSettings, QTimer, Signal
from PySide6.QtWidgets import QApplication

from fast_file_search import kinds
from fast_file_search.engine.protocol import (
    FOLDER, EngineConfig, FilterState, IndexStatus, ItemInfo, Ready, ResultRow, RootStatus,
    SearchRequest, SearchResult, SortSpec, Suggestion,
)

SEP = os.sep


# --------------------------------------------------------------------------- dataset
@dataclass
class Entry:
    id: int
    root_id: int
    rel_dir: str       # '' = directly in the root ('/'-separated)
    name: str
    size: int = 0
    mtime: int = 1_700_000_000
    flags: int = 0


class FakeDataset:
    def __init__(self, roots: Sequence[Tuple[int, str]], entries: Sequence[Entry] = ()):
        self.roots: Dict[int, str] = dict(roots)       # id -> absolute path
        self.entries: List[Entry] = list(entries)
        self.history: List[str] = []
        self.favorites: set = set()

    def path_of(self, e: Entry) -> str:
        root = self.roots[e.root_id]
        parts = [root] + ([p for p in e.rel_dir.split("/") if p]) + [e.name]
        return SEP.join(parts) if not root.endswith(SEP) else root + SEP.join(parts[1:])

    def add(self, root_id: int, rel_dir: str, name: str, size: int = 0, mtime: int = 1_700_000_000,
            flags: int = 0) -> Entry:
        e = Entry(len(self.entries) + 1, root_id, rel_dir, name, size, mtime, flags)
        self.entries.append(e)
        return e

    # -- evaluation
    def _match_text(self, e: Entry, terms: List[str]) -> Optional[tuple]:
        low = e.name.lower()
        spans = []
        for t in terms:
            if any(c in t for c in "*?"):
                if not fnmatch.fnmatchcase(low, t):
                    return None
                continue
            i = low.find(t)
            if i < 0:
                return None
            spans.append((i, i + len(t)))
        return tuple(sorted(spans))

    def _filters(self, e: Entry, f: FilterState, root_scope: bool) -> bool:
        folder = bool(e.flags & FOLDER)
        k = kinds.kind_of(e.name, e.flags)
        if f.kinds:
            ok = ("folder" in f.kinds and folder) or ("file" in f.kinds and not folder) or (k in f.kinds)
            if not ok:
                return False
        if f.exts:
            ext = e.name.rsplit(".", 1)[-1].lower() if "." in e.name else ""
            if folder or ext not in f.exts:
                return False
        if f.size_min is not None or f.size_max is not None:
            if folder:
                return False
            if f.size_min is not None and e.size < f.size_min:
                return False
            if f.size_max is not None and e.size >= f.size_max:
                return False
        if f.mtime_min is not None and e.mtime < f.mtime_min:
            return False
        if f.mtime_max is not None and e.mtime >= f.mtime_max:
            return False
        if f.folder:
            p = self.path_of(e)
            if not p.lower().startswith(f.folder.lower().rstrip(SEP) + SEP):
                return False
        if f.path_contains and f.path_contains.lower() not in e.rel_dir.lower():
            return False
        for x in f.excludes:
            comps = [c.lower() for c in e.rel_dir.split("/") if c] + [e.name.lower()]
            if any(fnmatch.fnmatchcase(c, x.lower()) for c in comps):
                return False
        if f.favorites_only and self.path_of(e) not in self.favorites:
            return False
        if root_scope and f.root_ids and e.root_id not in f.root_ids:
            return False
        return True

    def search(self, req: SearchRequest) -> Tuple[SearchResult, list]:
        t0 = time.perf_counter()
        text = req.text.strip()
        terms = [t.lower() for t in text.split() if t]
        hits = []
        for e in self.entries:
            spans = self._match_text(e, terms)
            if spans is None or not self._filters(e, req.filters, root_scope=False):
                continue
            hits.append((e, spans))
        counts: Dict[int, int] = {}
        for e, _s in hits:
            counts[e.root_id] = counts.get(e.root_id, 0) + 1
        if req.filters.root_ids:
            hits = [(e, s) for e, s in hits if e.root_id in req.filters.root_ids]
        key = req.sort.key
        if key == "name":
            hits.sort(key=lambda h: (h[0].name.lower(), h[0].id), reverse=req.sort.descending)
        elif key == "size":
            hits.sort(key=lambda h: (-1 if h[0].flags & FOLDER else h[0].size, h[0].id), reverse=req.sort.descending)
        elif key == "mtime":
            hits.sort(key=lambda h: (h[0].mtime, h[0].id), reverse=req.sort.descending)
        elif key == "folder":
            hits.sort(key=lambda h: (h[0].rel_dir.lower(), h[0].name.lower()), reverse=req.sort.descending)
        else:
            t = terms[0] if terms else ""
            hits.sort(key=lambda h: (0 if h[0].name.lower().startswith(t) else 1, len(h[0].name), h[0].id))
        rows = [self.row(e, s) for e, s in hits]
        sugg: List[Suggestion] = []
        if req.suggestions:
            low = text.lower()
            for q in self.history:
                if q.lower().startswith(low) and q.lower() != low and len(sugg) < 3:
                    sugg.append(Suggestion("history", q, 1))
            if text:
                seen = {}
                for r in rows[:200]:
                    seen[r.name] = seen.get(r.name, 0) + 1
                for name, n in list(seen.items())[:5]:
                    if name != text:
                        sugg.append(Suggestion("name", name, n))
        if req.commit_history and text and rows:
            self.history = [text] + [h for h in self.history if h != text]
        res = SearchResult(gen=req.gen, total=len(rows), offset=0, rows=tuple(rows[:req.limit]),
                           elapsed_ms=(time.perf_counter() - t0) * 1000,
                           root_counts=tuple(sorted(counts.items())) if req.root_counts else (),
                           suggestions=tuple(sugg), terms=tuple(t for t in terms if "*" not in t))
        return res, rows

    def row(self, e: Entry, spans=()) -> ResultRow:
        path = self.path_of(e)
        return ResultRow(e.id, e.name, path, e.root_id, e.rel_dir.replace("/", SEP), e.size, e.mtime,
                         e.flags, spans, path in self.favorites)

    def status(self, state: str = "idle", live: str = "on", names: Optional[Dict[int, str]] = None,
               root_state: Optional[Dict[int, str]] = None, scanned: int = 0) -> IndexStatus:
        roots = []
        for rid, path in self.roots.items():
            n = sum(1 for e in self.entries if e.root_id == rid)
            st = (root_state or {}).get(rid, "ok")
            roots.append(RootStatus(id=rid, path=path, name=(names or {}).get(rid) or os.path.basename(path),
                                    state=st, entries=n, folders=0, bytes=0, denied_dirs=0,
                                    scanned_entries=scanned if st == "scanning" else 0,
                                    last_scan_end=None if st == "scanning" else 1_700_000_000))
        total = sum(r.entries for r in roots)
        return IndexStatus(state=state, roots=tuple(roots), total_entries=total, total_folders=0,
                           live=live, index_generation=1)


# --------------------------------------------------------------------------- client
class FakeEngineClient(QObject):
    ready = Signal(object)
    search_result = Signal(object)
    index_status = Signal(object)
    index_changed = Signal(int)
    item_info = Signal(object)
    notice = Signal(object)
    engine_state = Signal(str)

    def __init__(self, dataset: Optional[FakeDataset] = None, *, ready: bool = True,
                 delay_ms: int = 0, auto: bool = True, parent=None):
        super().__init__(parent)
        self.dataset = dataset or FakeDataset([])
        self.delay_ms = delay_ms
        self.auto = auto
        self.current_gen = 0
        self.is_ready = False
        self.last_status: Optional[IndexStatus] = None
        self.searches: List[SearchRequest] = []
        self.pages: List[Tuple[int, int, int]] = []
        self.configs: List[EngineConfig] = []
        self.index_calls: List[tuple] = []
        self.user_ops: List[tuple] = []
        self.forgotten: List[str] = []
        self.info_requests: List[str] = []
        self.started = 0
        self.held: List[object] = []
        self._full: Dict[int, list] = {}
        self.responder: Optional[Callable[[SearchRequest], Optional[SearchResult]]] = None
        if ready:
            self.make_ready()

    # -- helpers for tests
    def make_ready(self, status: Optional[IndexStatus] = None) -> None:
        st = status or self.dataset.status()
        self.is_ready = True
        self.last_status = st
        self._emit(Ready(protocol_version=1, status=st), self.ready, delay=0)

    def push_status(self, status: IndexStatus) -> None:
        self.last_status = status
        self.index_status.emit(status)

    def _emit(self, obj, signal, delay: Optional[int] = None) -> None:
        d = self.delay_ms if delay is None else delay
        if not self.auto and signal is self.search_result:
            self.held.append(obj)
            return
        QTimer.singleShot(d, lambda: signal.emit(obj))

    def deliver(self, index: Optional[int] = None) -> None:
        items = self.held if index is None else [self.held[index]]
        for obj in list(items):
            self.held.remove(obj)
            self.search_result.emit(obj)

    @property
    def last(self) -> SearchRequest:
        return self.searches[-1]

    # -- EngineClient API
    def start(self) -> None:
        self.started += 1

    def shutdown(self, timeout: float = 3.0) -> None:
        pass

    def configure(self, cfg: EngineConfig) -> None:
        self.configs.append(cfg)

    def search(self, text: str, filters: FilterState, sort: SortSpec, *, limit: int = 200,
               suggestions: bool = True, commit_history: bool = False) -> int:
        self.current_gen += 1
        req = SearchRequest(gen=self.current_gen, text=text, filters=filters, sort=sort, limit=limit,
                            suggestions=suggestions, commit_history=commit_history)
        self.searches.append(req)
        if self.responder is not None:
            res = self.responder(req)
        else:
            res, rows = self.dataset.search(req)
            self._full[req.gen] = rows
        if res is not None:
            self._emit(res, self.search_result)
        return req.gen

    def fetch_page(self, gen: int, offset: int, limit: int = 500) -> None:
        self.pages.append((gen, offset, limit))
        rows = self._full.get(gen, [])
        res = SearchResult(gen=gen, total=len(rows), offset=offset, rows=tuple(rows[offset:offset + limit]),
                           elapsed_ms=1.0)
        self._emit(res, self.search_result)

    def index(self, action: str, root_ids=None) -> None:
        self.index_calls.append((action, root_ids))

    def user_op(self, op: str, paths=(), value=None) -> None:
        self.user_ops.append((op, tuple(paths), value))
        if op == "favorite":
            self.dataset.favorites.update(paths)
        elif op == "unfavorite":
            self.dataset.favorites.difference_update(paths)

    def request_item_info(self, path: str) -> int:
        self.info_requests.append(path)
        req_id = len(self.info_requests)
        info = ItemInfo(req_id=req_id, path=path, favorite=path in self.dataset.favorites,
                        tags=("work",), note="a note")
        QTimer.singleShot(0, lambda: self.item_info.emit(info))
        return req_id

    def forget(self, paths) -> None:
        self.forgotten.extend(paths)


# --------------------------------------------------------------------------- sample data
def sample_dataset(base: str) -> FakeDataset:
    """Downloads / Desktop / Documents / Projects with "GiftTopIcon" assets, as in the
    reference screenshot."""
    roots = [(1, os.path.join(base, "Downloads")), (2, os.path.join(base, "Desktop")),
             (3, os.path.join(base, "Documents")), (4, os.path.join(base, "Projects"))]
    ds = FakeDataset(roots)
    now = 1_760_000_000
    for i, (folder, name, size) in enumerate([
        ("icons", "GiftTopIcon.png", 4_812), ("icons", "GiftTopIcon@2x.png", 9_406),
        ("icons", "GiftTopIcon.svg", 2_114), ("icons/old", "GiftTopIcon-v1.png", 3_902),
        ("ui-kit/assets", "GiftTopIcon.pdf", 8_240), ("ui-kit/assets", "GiftTopIconDark.png", 5_120),
        ("ui-kit/assets", "GiftTopIconLight.png", 5_002), ("", "GiftTopIcon (1).png", 4_812),
        ("", "GiftTopIcon (2).png", 4_812), ("exports/2025", "GiftTopIcon_export.png", 7_731),
        ("exports/2025", "GiftTopIcon_export.webp", 3_310), ("exports/2025", "gifttopicon-notes.txt", 612),
        ("sketch", "GiftTopIcon.sketch.json", 6_300), ("sketch", "GiftTopIcon.ico", 1_150),
        ("zips", "GiftTopIcon-assets.zip", 9_998), ("icons", "GiftTopIconMask.png", 812),
        ("icons", "GiftTopIcon_large.png", 48_211), ("video", "GiftTopIcon-anim.mp4", 1_204_311),
    ]):
        ds.add(1, folder, name, size, now - i * 7200)
    for i in range(6):
        ds.add(2, "design", f"GiftTopIcon draft {i + 1}.png", 3_000 + i * 900, now - 86400 * i)
    ds.add(3, "Work/Brand", "GiftTopIcon guidelines.docx", 9_100, now - 86400 * 20)
    ds.add(3, "Work/Brand", "GiftTopIcon usage.md", 2_048, now - 86400 * 21)
    ds.add(4, "web/src/components", "GiftTopIcon.tsx", 1_934, now - 3600)
    ds.add(4, "web/src/components", "GiftTopIcon.test.tsx", 1_288, now - 3700)
    ds.add(4, "web/node_modules/icons", "GiftTopIcon.js", 900, now - 3800)
    ds.add(4, "web/src", "GiftTopIcon.tmp", 400, now - 3900)
    words = ["report", "invoice", "photo", "notes", "budget", "draft", "summary", "plan", "index", "main"]
    exts = ["pdf", "docx", "jpg", "txt", "xlsx", "md", "py", "png", "zip", "js"]
    for rid, n in ((1, 120), (2, 90), (3, 150), (4, 110)):
        for i in range(n):
            w = words[i % len(words)]
            ds.add(rid, ["", "a", "a/b", "misc"][i % 4], f"{w}_{i}.{exts[(i * 7) % len(exts)]}",
                   (i * 7919) % 4_000_000, now - i * 50_000)
        ds.add(rid, "", "misc", 0, now, FOLDER)
    return ds


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


class FakeConfigManager:
    """ConfigManager stand-in: real AppConfig, save() records instead of writing."""

    def __init__(self, paths: Sequence[str] = ()):
        from fast_file_search.config import AppConfig

        self.config = AppConfig(index_paths=list(paths))
        self.saves = 0
        self.first_run = False

    def save(self) -> None:
        self.saves += 1


@pytest.fixture
def ui_settings(tmp_path):
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))
    yield tmp_path / "qsettings"


@pytest.fixture
def dataset(tmp_path):
    return sample_dataset(str(tmp_path / "home"))


@pytest.fixture
def client(qapp, dataset):
    return FakeEngineClient(dataset)


@pytest.fixture
def make_window(qapp, ui_settings, qtbot):
    from fast_file_search.ui.main_window import MainWindow

    made = []

    def make(client, paths=None, first_run=False, show=True):
        cm = FakeConfigManager(list(paths) if paths is not None else list(client.dataset.roots.values()))
        win = MainWindow(client, cm, first_run=first_run)
        win.cm = cm
        made.append(win)
        if show:
            win.show()
            qtbot.waitExposed(win)
            win.activateWindow()
        qtbot.waitUntil(lambda: client.last_status is None or win._status is not None, timeout=2000)
        return win

    yield make
    for w in made:
        w._shutdown()
        w.close()
        w.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def window(make_window, client):
    return make_window(client)


def type_text(qtbot, widget, text, delay=0):
    from PySide6.QtTest import QTest

    for ch in text:
        QTest.keyClicks(widget, ch)
        if delay:
            qtbot.wait(delay)


def wait_result(qtbot, win, timeout=2000):
    """Wait until the window shows the result for its latest request."""
    qtbot.waitUntil(lambda: win._displayed_key == win._req_key and win._last_result is not None
                    and win._last_result.gen == win._gen, timeout=timeout)


def search(qtbot, win, text):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    f = win.bar.field
    f.setFocus()
    f.selectAll()
    QTest.keyClicks(f, text)
    QTest.keyClick(f, Qt.Key.Key_Return)
    wait_result(qtbot, win)


@pytest.fixture
def fake_os(monkeypatch):
    """Replace OS actions (open, reveal, open with, trash, delete) with recorders."""
    from fast_file_search.ui import file_actions

    calls = []
    monkeypatch.setattr(file_actions, "open_path", lambda p: calls.append(("open", p)))
    monkeypatch.setattr(file_actions, "reveal", lambda p: calls.append(("reveal", p)))
    monkeypatch.setattr(file_actions, "open_with", lambda p, a=None: calls.append(("open_with", p)))
    monkeypatch.setattr(file_actions, "move_to_trash", lambda p: (calls.append(("trash", p)), (True, None))[1])
    monkeypatch.setattr(file_actions, "delete_permanently", lambda p: calls.append(("delete", p)))
    return calls


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """A modal dialog would block the offscreen run forever: record it and return."""
    from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

    shown = []

    def box(kind):
        def f(parent, title, text, *a, **k):
            shown.append((kind, title, text))
            return QMessageBox.StandardButton.Ok
        return staticmethod(f)

    for kind in ("warning", "information", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind, box(kind))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: (shown.append(("exec", self.text(), "")), 0)[1])
    monkeypatch.setattr(QDialog, "exec", lambda self: (shown.append(("dialog", type(self).__name__, "")), 0)[1])
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    monkeypatch.setattr(QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("", False)))
    return shown
