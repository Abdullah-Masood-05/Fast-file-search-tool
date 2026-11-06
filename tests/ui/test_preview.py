"""Preview pane: header, worker behaviour, hashes, images, breadcrumb, gutter."""
import ast
import hashlib
import inspect
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from fast_file_search.engine.protocol import DATALESS, FOLDER, ItemInfo, ResultRow
from fast_file_search.ui import code_view, highlight, preview
from fast_file_search.ui.preview import Breadcrumb, KindBadge, PreviewPane, breadcrumb_segments, decode_text


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeClient(QObject):
    item_info = Signal(object)

    def __init__(self):
        super().__init__()
        self.requests = []

    def request_item_info(self, path):
        self.requests.append(path)
        return len(self.requests)


@pytest.fixture
def pane(qapp, qtbot):
    client = FakeClient()
    p = PreviewPane(client)
    p.client = client
    p.resize(420, 640)
    qtbot.addWidget(p)
    p.show()
    qtbot.waitExposed(p)
    p.layout().activate()
    yield p
    p.shutdown()


def make_row(path: Path, flags=0, size=None, mtime=None) -> ResultRow:
    st = path.stat()
    return ResultRow(1, path.name, str(path), 1, "", st.st_size if size is None else size,
                     int(st.st_mtime) if mtime is None else mtime, flags, (), False)


def wait_text(qtbot, pane, timeout=3000):
    qtbot.waitUntil(lambda: pane.stack.currentIndex() == pane.PAGE_CODE and pane.code.toPlainText() != "",
                    timeout=timeout)


def test_decode_text_variants():
    assert decode_text(b"hello") == "hello"
    assert decode_text(b"\xef\xbb\xbfhi") == "hi"
    assert decode_text("héllo".encode("utf-16")) == "héllo"
    assert decode_text(b"a\x00b") is None
    assert decode_text(bytes(range(1, 8)) * 40) is None
    assert decode_text(b"caf\xc3") == "caf�"
    assert decode_text(b"caf\xc3", final=False) == "caf"  # split multibyte at the 64 KB cut


def test_text_preview_and_highlight(pane, qtbot, tmp_path):
    f = tmp_path / "app.js"
    f.write_text("const a = 1; // hi\nlet s = 'x';\n", encoding="utf-8")
    pane.show_row(make_row(f), str(tmp_path))
    wait_text(qtbot, pane)
    assert pane.code.toPlainText().startswith("const a")
    assert pane.code.highlighter.language == "js"
    assert pane.badge.text() == "JS"
    assert pane.name_label.full_text() == "app.js"
    assert pane.kind_label.full_text() == "JavaScript"


def test_header_updates_within_2ms(pane, qtbot, tmp_path, perf_scale):
    rows = []
    for i in range(30):
        f = tmp_path / f"file{i}.py"
        f.write_text("x = 1\n")
        rows.append(make_row(f))
    pane.show_row(rows[0], str(tmp_path))  # warm up (creates the breadcrumb buttons)
    times = []
    for r in rows[1:]:
        t = time.perf_counter()
        pane.show_row(r, str(tmp_path))
        times.append((time.perf_counter() - t) * 1000)
        assert pane.name_label.full_text() == r.name
    times.sort()
    print(f"show_row header: median {times[len(times) // 2]:.2f} ms, max {times[-1]:.2f} ms")
    assert times[len(times) // 2] <= 2.0 * perf_scale


def test_64kb_text_preview_within_30ms(pane, qtbot, tmp_path, qapp, perf_scale):
    f = tmp_path / "big.txt"
    # Over the 64 KB limit on every platform (write_text would add \r on Windows only).
    f.write_bytes(("The quick brown fox jumps over the lazy dog.\n" * 2000)[:66000].encode("utf-8"))
    row = make_row(f)
    other = tmp_path / "other.txt"
    other.write_text("warm")
    pane.show_row(make_row(other), str(tmp_path))
    wait_text(qtbot, pane)
    best = 1e9
    for _ in range(3):
        pane.show_row(make_row(other), str(tmp_path))
        qtbot.waitUntil(lambda: pane.code.toPlainText() == "warm", timeout=2000)
        t = time.perf_counter()
        pane.show_row(row, str(tmp_path))
        while pane.code.toPlainText() == "warm":
            qapp.processEvents()
        best = min(best, (time.perf_counter() - t) * 1000)
    print(f"64 KB text preview: {best:.1f} ms")
    assert best <= 30 * perf_scale
    assert pane.note.isVisible() or pane.note.text()  # truncation / line-limit note


def test_arrowing_through_50_rows_uses_one_worker(pane, qtbot, tmp_path, qapp):
    rows = []
    for i in range(50):
        f = tmp_path / f"n{i:02d}.txt"
        f.write_text(f"file {i}\n" * 50)
        rows.append(make_row(f))
    pane.show_row(rows[0], str(tmp_path))
    qtbot.waitUntil(lambda: pane.code.toPlainText().startswith("file 0"), timeout=3000)
    threads_before = threading.active_count()
    for r in rows[1:]:
        pane.show_row(r, str(tmp_path))
        qapp.processEvents()
    qtbot.waitUntil(lambda: pane.code.toPlainText().startswith("file 49"), timeout=3000)
    assert threading.active_count() == threads_before
    assert pane._worker.max_concurrent == 1
    assert pane._worker.processed < 51 + 5  # no more than one request per selection


def test_stale_results_are_dropped(pane, qtbot, tmp_path, qapp):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("AAA")
    b.write_text("BBB")
    pane.show_row(make_row(a), None)
    pane.show_row(make_row(b), None)
    qtbot.waitUntil(lambda: pane.code.toPlainText() == "BBB", timeout=3000)
    qtbot.wait(50)
    assert pane.code.toPlainText() == "BBB"


def test_hashes_only_while_details_visible(pane, qtbot, tmp_path):
    f = tmp_path / "data.bin"
    payload = b"abc" * 1000
    f.write_bytes(payload)
    pane.show_row(make_row(f), str(tmp_path))
    qtbot.waitUntil(lambda: pane._loaded_sel == pane._sel, timeout=3000)
    qtbot.wait(100)
    assert pane._worker.hash_chunks == 0
    assert pane.values["MD5"].value() == ""
    pane.tabs.setCurrentIndex(pane.TAB_DETAILS)
    qtbot.waitUntil(lambda: pane.values["MD5"].value() == hashlib.md5(payload).hexdigest(), timeout=3000)
    assert pane.values["SHA-256"].value() == hashlib.sha256(payload).hexdigest()
    # leaving the tab resets nothing for the finished file, and new selections do not hash
    pane.tabs.setCurrentIndex(pane.TAB_PREVIEW)
    chunks = pane._worker.hash_chunks
    g = tmp_path / "other.bin"
    g.write_bytes(b"zz")
    pane.show_row(make_row(g), str(tmp_path))
    qtbot.wait(150)
    assert pane._worker.hash_chunks == chunks


def test_hash_cache_reuses_result(pane, qtbot, tmp_path):
    f = tmp_path / "c.bin"
    f.write_bytes(b"1" * 5000)
    pane.tabs.setCurrentIndex(pane.TAB_DETAILS)
    pane.show_row(make_row(f), None)
    qtbot.waitUntil(lambda: len(pane.values["MD5"].value()) == 32, timeout=3000)
    chunks = pane._worker.hash_chunks
    pane.show_row(None, None)
    pane.show_row(make_row(f), None)
    qtbot.waitUntil(lambda: len(pane.values["MD5"].value()) == 32, timeout=3000)
    assert pane._worker.hash_chunks == chunks


def test_no_hashes_for_dataless_folders_or_huge_files(pane, qtbot, tmp_path):
    pane.tabs.setCurrentIndex(pane.TAB_DETAILS)
    f = tmp_path / "cloud.txt"
    f.write_text("x")
    pane.show_row(make_row(f, flags=DATALESS), None)
    assert "Not downloaded" in pane.values["MD5"].value()
    pane.show_row(make_row(f, size=600 * 1024 * 1024), None)
    assert "512 MB" in pane.values["SHA-256"].value()
    d = tmp_path / "dir"
    d.mkdir()
    pane.show_row(make_row(d, flags=FOLDER), None)
    assert "folders" in pane.values["MD5"].value()
    qtbot.wait(150)
    assert pane._worker.hash_chunks == 0


def test_selection_change_cancels_hashing_within_a_chunk(pane, qtbot, tmp_path):
    big = tmp_path / "big.bin"
    with open(big, "wb") as fh:
        chunk = b"\x01" * (1024 * 1024)
        for _ in range(300):
            fh.write(chunk)
    small = tmp_path / "small.txt"
    small.write_text("s")
    pane.tabs.setCurrentIndex(pane.TAB_DETAILS)
    pane.show_row(make_row(big), None)
    qtbot.waitUntil(lambda: pane._worker.hash_chunks >= 2, timeout=5000)
    at_switch = pane._worker.hash_chunks
    pane.show_row(make_row(small), None)
    qtbot.wait(300)
    final = pane._worker.hash_chunks
    print(f"hash chunks after switch: {final - at_switch}")
    assert final - at_switch <= 3  # one chunk in flight + counter race, not the remaining ~290
    assert pane.values["MD5"].value() != ""  # small file's own hash (or in progress)


def test_item_info_fills_tags_and_note(pane, qtbot, tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("x")
    pane.tabs.setCurrentIndex(pane.TAB_DETAILS)
    pane.show_row(make_row(f), None)
    qtbot.waitUntil(lambda: pane.client.requests == [str(f)], timeout=2000)
    assert not pane.details_form.isRowVisible(pane._rows["Tags"])
    pane.client.item_info.emit(ItemInfo(1, str(f), False, ("work", "todo"), "remember"))
    assert pane.values["Tags"].text() == "work, todo"
    assert pane.details_form.isRowVisible(pane._rows["Note"])
    pane.client.item_info.emit(ItemInfo(2, "elsewhere", False, ("x",), "y"))
    assert pane.values["Tags"].text() == "work, todo"


def test_image_preview_scaled_to_fit(pane, qtbot, tmp_path):
    img = QImage(1600, 900, QImage.Format.Format_RGB32)
    img.fill(QColor("#3070c0"))
    f = tmp_path / "pic.png"
    assert img.save(str(f))
    pane.show_row(make_row(f), None)
    qtbot.waitUntil(lambda: pane.stack.currentIndex() == pane.PAGE_IMAGE, timeout=3000)
    pm = pane.image_view.picture.pixmap()
    assert not pm.isNull()
    target = pane.image_view.target_size(pane.stack.size())
    assert pm.width() / pm.devicePixelRatio() <= target.width() + 1
    assert "1600 × 900" in pane.image_view.caption.text()
    assert "1600 × 900" in pane.values["Dimensions"].text()


def test_messages_for_binary_folder_missing_dataless(pane, qtbot, tmp_path):
    b = tmp_path / "x.dat"
    b.write_bytes(b"\x00\x01\x02" * 100)
    pane.show_row(make_row(b), None)
    qtbot.waitUntil(lambda: "No preview" in pane.message_view.label.text(), timeout=3000)
    assert pane.stack.currentIndex() == pane.PAGE_MESSAGE

    d = tmp_path / "folder"
    d.mkdir()
    for i in range(3):
        (d / f"{i}").write_text("")
    pane.show_row(make_row(d, flags=FOLDER), None)
    qtbot.waitUntil(lambda: pane.message_view.label.text().startswith("Folder with 3"), timeout=3000)

    e = tmp_path / "empty.txt"
    e.write_text("")
    pane.show_row(make_row(e), None)
    qtbot.waitUntil(lambda: "empty" in pane.message_view.label.text(), timeout=3000)

    ghost = ResultRow(9, "gone.txt", str(tmp_path / "gone.txt"), 1, "", 5, 0, 0, (), False)
    pane.show_row(ghost, None)
    qtbot.waitUntil(lambda: "no longer exists" in pane.message_view.label.text(), timeout=3000)

    cloud = tmp_path / "icloud.txt"
    cloud.write_text("x")
    pane.show_row(make_row(cloud, flags=DATALESS), None)
    qtbot.waitUntil(lambda: "Not downloaded" in pane.message_view.label.text(), timeout=3000)


def test_text_over_limits_is_not_highlighted(pane, qtbot, tmp_path):
    f = tmp_path / "wide.py"
    f.write_text("x = 1\n" + "y" * 2500 + "\n")
    pane.show_row(make_row(f), None)
    wait_text(qtbot, pane)
    assert pane.code.highlighter.language is None
    g = tmp_path / "long.py"
    g.write_text("a = 1\n" * 3000)
    pane.show_row(make_row(g), None)
    qtbot.waitUntil(lambda: pane.note.text().startswith("Showing the first 2,000"), timeout=3000)
    assert pane.code.blockCount() == 2000


def test_buttons_emit_signals(pane, qtbot, tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("x")
    pane.show_row(make_row(f), None)
    got = []
    pane.open_requested.connect(lambda p: got.append(("open", p)))
    pane.reveal_requested.connect(lambda p: got.append(("reveal", p)))
    pane.copy_path_requested.connect(lambda p: got.append(("copy", p)))
    for b in (pane.open_button, pane.reveal_button, pane.copy_button):
        b.click()
    assert got == [("open", str(f)), ("reveal", str(f)), ("copy", str(f))]
    pane.show_row(None, None)
    assert not pane.open_button.isEnabled()


def test_set_terms_marks_matches(pane, qtbot, tmp_path):
    f = tmp_path / "m.txt"
    f.write_text("foo bar foo\nbaz\n")
    pane.set_terms(["foo"])
    pane.show_row(make_row(f), None)
    wait_text(qtbot, pane)
    assert len(pane.code.extraSelections()) == 2


def test_breadcrumb_segments_and_clicks(qapp, qtbot, tmp_path):
    deep = tmp_path / "Downloads" / "Compressed" / "Ebad"
    deep.mkdir(parents=True)
    segs = breadcrumb_segments(str(deep), str(tmp_path / "Downloads"))
    assert [s[0] for s in segs] == ["Downloads", "Compressed", "Ebad"]
    assert segs[1][1] == str(tmp_path / "Downloads" / "Compressed")
    bc = Breadcrumb()
    qtbot.addWidget(bc)
    bc.resize(600, 28)
    bc.show()
    bc.set_path(str(deep), str(tmp_path / "Downloads"))
    assert bc.visible_labels() == ["Downloads", "Compressed", "Ebad"]
    with qtbot.waitSignal(bc.scope_requested) as sig:
        bc._buttons[1].click()
    assert sig.args == [str(tmp_path / "Downloads" / "Compressed")]


def test_breadcrumb_collapses_middle_when_narrow(qapp, qtbot, tmp_path):
    deep = tmp_path.joinpath(*[f"folder{i}" for i in range(9)])
    bc = Breadcrumb()
    qtbot.addWidget(bc)
    bc.resize(260, 28)
    bc.show()
    bc.set_path(str(deep), None)
    labels = bc.visible_labels()
    assert labels[0] == segs0(deep) and labels[-1] == "folder8"
    assert bc._more.isVisible() and len(bc._hidden) > 0
    assert len(labels) < 10
    for b in bc._buttons:
        if not b.isHidden():
            assert b.geometry().right() <= bc.width()


def segs0(path: Path) -> str:
    return breadcrumb_segments(str(path))[0][0]


def test_kind_badge_text_and_folder(qapp):
    badge = KindBadge()
    badge.set_badge("JS", 0)
    assert badge.text() == "JS" and badge.size().width() == 36 == badge.size().height()
    badge.set_badge("", FOLDER)
    assert badge.grab().toImage().width() >= 36


def test_gutter_follows_scroll_and_resize(qapp, qtbot):
    view = code_view.CodeView()
    qtbot.addWidget(view)
    view.resize(500, 300)
    view.show()
    view.set_content("\n".join(f"line {i}" for i in range(1, 801)), None)
    qtbot.wait(20)
    gutter = view._gutter
    gutter.grab()
    assert view.painted_numbers[0] == 1
    view.verticalScrollBar().setValue(200)
    qapp.processEvents()
    gutter.grab()
    first = view.firstVisibleBlock().blockNumber() + 1
    assert view.painted_numbers[0] == first > 1
    assert view.painted_numbers == list(range(first, first + len(view.painted_numbers)))
    assert view.painted_numbers[-1] >= first + 5
    # resize: gutter keeps the full content height and the viewport margin matches its width
    view.resize(700, 480)
    qapp.processEvents()
    assert gutter.geometry().height() == view.contentsRect().height()
    assert gutter.width() == view.gutter_width() == view.viewportMargins().left()
    gutter.grab()
    assert len(view.painted_numbers) > 10
    # 5-digit documents get a wider gutter
    narrow = view.gutter_width()
    view.set_content("\n" * 12000, None)
    assert view.gutter_width() > narrow


def test_no_sqlite_and_no_pixmap_off_gui_thread():
    for mod in (preview, code_view, highlight):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] != "sqlite3" for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "sqlite3"
                assert "persistent_index" not in (node.module or "") and "store" not in (node.module or "")
    worker_src = inspect.getsource(preview.PreviewWorker)
    assert "QPixmap" not in worker_src


def test_pixmap_conversion_happens_on_gui_thread(pane, qtbot, tmp_path, monkeypatch):
    seen = []
    real = preview.QPixmap.fromImage

    def spy(*a, **k):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)

    monkeypatch.setattr(preview.QPixmap, "fromImage", staticmethod(spy))
    img = QImage(64, 64, QImage.Format.Format_RGB32)
    img.fill(QColor("#999999"))
    f = tmp_path / "p.png"
    img.save(str(f))
    pane.show_row(make_row(f), None)
    qtbot.waitUntil(lambda: bool(seen), timeout=3000)
    assert seen == [True]


def test_shutdown_stops_worker(qapp, qtbot, tmp_path):
    p = PreviewPane(FakeClient())
    qtbot.addWidget(p)
    f = tmp_path / "q.txt"
    f.write_text("x")
    p.show_row(make_row(f), None)
    assert p._worker.isRunning()
    p.shutdown()
    assert not p._worker.isRunning()


def test_highlighted_python_preview_latency(pane, qtbot, tmp_path, qapp, perf_scale):
    block = 'def f(a, b=2):\n    """doc"""\n    return a + b  # sum\n'
    f = tmp_path / "big.py"
    f.write_text((block * 1000)[:65536])
    other = tmp_path / "o.py"
    other.write_text("x = 1\n")
    best = 1e9
    for _ in range(3):
        pane.show_row(make_row(other), None)
        qtbot.waitUntil(lambda: pane.code.toPlainText() == "x = 1\n", timeout=2000)
        t = time.perf_counter()
        pane.show_row(make_row(f), None)
        while pane.code.toPlainText() == "x = 1\n":
            qapp.processEvents()
        best = min(best, (time.perf_counter() - t) * 1000)
    print(f"64 KB highlighted python preview: {best:.1f} ms")
    assert pane.code.highlighter.language == "python"
    assert best <= 30 * perf_scale


def test_palette_change_rebuilds_highlight_formats(qapp, qtbot):
    from fast_file_search.ui import theme

    view = code_view.CodeView()
    qtbot.addWidget(view)
    view.set_content("def f(): pass\n", "python")
    before = view.highlighter.formats["keyword"].foreground().color().name()
    view.setPalette(theme._fallback_palette(True, QColor("#0067c0")))
    qapp.processEvents()
    after = view.highlighter.formats["keyword"].foreground().color().name()
    assert before != after
