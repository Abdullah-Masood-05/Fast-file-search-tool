"""Main window: structure, keyboard map (F.8), states (F.9), status bar (F.7), actions,
accessibility (F.10), persistence and the no-SQL / no-thread rules (H.3)."""
import ast
import os
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QScrollBar,
    QTabBar, QWidget,
)

from fast_file_search.engine.protocol import IndexStatus, Notice, SearchResult
from fast_file_search.ui import theme

from fake_engine import *  # noqa: F401,F403
from fake_engine import FakeEngineClient, search, wait_result

UI_DIR = Path(__file__).resolve().parents[2] / "src" / "fast_file_search" / "ui"


# ---------------------------------------------------------------- structure
def test_native_window_menus_and_roles(window):
    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.windowTitle() == "Fast File Search Pro"
    menus = [a.text().replace("&", "") for a in window.menuBar().actions()]
    assert menus == ["File", "Edit", "View", "Index", "Help"]
    assert window.act_settings.menuRole() == QAction.MenuRole.PreferencesRole
    assert window.act_quit.menuRole() == QAction.MenuRole.QuitRole
    assert window.act_about.menuRole() == QAction.MenuRole.AboutRole
    assert window.minimumWidth() == 900 and window.minimumHeight() == 560
    sizes = window.main_splitter.sizes()
    assert len(sizes) == 3 and all(s > 0 for s in sizes)


def test_keyboard_map_shortcuts(window):
    def has(act, seq):
        return QKeySequence(seq) in act.shortcuts()

    for seq in ("Ctrl+K", "Ctrl+L"):
        assert has(window.act_find, seq)
    assert QKeySequence(QKeySequence.StandardKey.Find) in window.act_find.shortcuts()
    assert has(window.act_reveal, "Ctrl+Return")
    assert QKeySequence(QKeySequence.StandardKey.Copy) in window.act_copy_path.shortcuts()
    assert has(window.act_copy_name, "Ctrl+Shift+C")
    assert has(window.act_clear_filters, "Ctrl+Shift+X")
    assert has(window.act_update_index, "F5") or has(window.act_update_index, "Ctrl+R")
    assert has(window.act_update_index, "Ctrl+R")
    assert has(window.act_settings, "Ctrl+,")
    assert QKeySequence(QKeySequence.StandardKey.Delete) in window.act_trash.shortcuts()
    assert window.act_trash.shortcutContext() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    for n in range(10):
        assert has(window.root_actions[n], f"Ctrl+{n}")


def test_focus_starts_in_search_and_find_selects_all(window):
    assert window.focusWidget() is window.bar.field
    window.bar.field.setText("abc")
    window.results.view.setFocus()
    window.act_find.trigger()
    assert window.focusWidget() is window.bar.field and window.bar.field.selectedText() == "abc"


def test_focus_search_with_query_runs_it(window, client, qtbot):
    window.focus_search("invoice")
    assert window.bar.text() == "invoice"
    assert client.last.text == "invoice" and client.last.commit_history
    wait_result(qtbot, window)
    assert window.results.is_showing_results()


def test_tab_order(window, qtbot):
    search(qtbot, window, "gifttopicon")
    chain = [window.bar.field, window.bar.search_btn, window.chips.type_chip.button]
    w = window.bar.field
    for expected in chain[1:]:
        w = _next_tab(w)
        assert w is expected
    w = window.chips.clear_btn
    assert _next_tab(w) is window.sidebar.list
    assert _next_tab(window.sidebar.list) is window.sidebar.smart_list
    assert _next_tab(window.results.sort_button) is window.results.view
    nxt = _next_tab(window.results.view)
    assert window.preview.isAncestorOf(nxt) or nxt is window.preview


def _next_tab(w: QWidget) -> QWidget:
    n = w.nextInFocusChain()
    while n is not w and not (n.focusPolicy() & Qt.FocusPolicy.TabFocus and n.isVisible() and n.isEnabled()):
        n = n.nextInFocusChain()
    return n


# ---------------------------------------------------------------- keyboard flow
def test_enter_ctrl_enter_up_and_esc_in_results(window, qtbot, fake_os):
    search(qtbot, window, "gifttopicon")
    view = window.results.view
    QTest.keyClick(window.bar.field, Qt.Key.Key_Down)
    first = window.results.current_row().path
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert fake_os[-1] == ("open", first)
    QTest.keyClick(view, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert fake_os[-1] == ("reveal", first)
    QTest.keyClick(view, Qt.Key.Key_Up)                # Up on the first row: back to the field
    assert window.focusWidget() is window.bar.field
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert window.focusWidget() is window.bar.field and window.bar.text() == "gifttopicon"


def test_copy_path_and_name(window, qtbot):
    search(qtbot, window, "gifttopicon")
    window.results.select_row(0)
    row = window.results.current_row()
    window.bar.field.setFocus()
    window.bar.field.deselect()
    QTest.keyClick(window.bar.field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)   # no selection
    assert QGuiApplication.clipboard().text() == row.path
    window.act_copy_name.trigger()
    assert QGuiApplication.clipboard().text() == row.name
    window.bar.field.selectAll()
    QTest.keyClick(window.bar.field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)   # text selected
    assert QGuiApplication.clipboard().text() == "gifttopicon"


def test_delete_trashes_only_from_results_with_confirmation(window, client, qtbot, fake_os, monkeypatch):
    search(qtbot, window, "gifttopicon")
    window.results.select_row(0)
    path = window.results.current_row().path
    window.bar.field.setFocus()
    QTest.keyClick(window.bar.field, Qt.Key.Key_Delete)
    assert ("trash", path) not in fake_os                         # the field keeps Delete
    monkeypatch.setattr(window, "_confirm", lambda *a, **k: False)
    window.results.view.setFocus()
    QTest.keyClick(window.results.view, Qt.Key.Key_Delete)
    assert ("trash", path) not in fake_os                         # cancelled
    monkeypatch.setattr(window, "_confirm", lambda *a, **k: True)
    n = window.results.model.rowCount()
    QTest.keyClick(window.results.view, Qt.Key.Key_Delete)
    assert fake_os[-1] == ("trash", path)
    assert window.results.model.rowCount() == n - 1 and client.forgotten == [path]


def test_trash_unavailable_offers_permanent_delete_only_after_confirmation(window, client, qtbot, monkeypatch):
    from fast_file_search.ui import file_actions

    search(qtbot, window, "invoice_1.")
    path = window.results.model.row(0).path
    calls = []
    monkeypatch.setattr(file_actions, "move_to_trash", lambda p: (False, None))
    monkeypatch.setattr(file_actions, "delete_permanently", lambda p: calls.append(p))
    answers = iter([True, False])
    asked = []
    monkeypatch.setattr(window, "_confirm", lambda *a, **k: (asked.append(a[0]), next(answers))[1])
    window.trash_paths([path])
    assert calls == [] and asked[1] == "Delete permanently" and window.results.model.row_of_path(path) >= 0
    answers = iter([True, True])
    window.trash_paths([path])
    assert calls == [path] and window.results.model.row_of_path(path) < 0 and path in client.forgotten


def test_os_errors_are_reported_in_plain_language(window, no_modal_dialogs):
    window.open_paths(["/definitely/not/here.txt"])
    assert any("no longer exists" in e[2] for e in no_modal_dialogs)


def test_context_menu_items_favorites_tags_notes(window, client, qtbot, fake_os, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    from fast_file_search.ui import file_actions

    search(qtbot, window, "gifttopicon")
    window.results.select_row(0)
    rows = window.results.selected_rows()
    menu = window.build_context_menu(rows)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    for expected in ("Open", theme.reveal_label(), "Copy path", "Copy name", "Add to favorites",
                     "Edit tags…", "Edit note…", theme.trash_label()):
        assert expected in texts
    if file_actions.open_with_mode() in ("system", "choose-app"):
        assert "Open with…" in texts
    act = lambda text: next(a for a in menu.actions() if a.text() == text)
    act("Open").trigger()
    assert fake_os[-1] == ("open", rows[0].path)
    act("Add to favorites").trigger()
    assert client.user_ops[-1] == ("favorite", (rows[0].path,), None)
    assert window.results.model.row(0).favorite
    assert "Remove from favorites" in [a.text() for a in window.build_context_menu(window.results.selected_rows()).actions()]

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("work, urgent ", True)))
    act("Edit tags…").trigger()
    qtbot.waitUntil(lambda: client.user_ops[-1][0] == "set_tags", timeout=1000)
    assert client.user_ops[-1] == ("set_tags", (rows[0].path,), ["work", "urgent"])
    monkeypatch.setattr(QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: (" hello ", True)))
    act("Edit note…").trigger()
    qtbot.waitUntil(lambda: client.user_ops[-1][0] == "set_note", timeout=1000)
    assert client.user_ops[-1] == ("set_note", (rows[0].path,), "hello")

    window.results.view.selectAll()
    plural = [a.text() for a in window.build_context_menu(window.results.selected_rows()).actions()]
    assert "Copy paths" in plural and "Copy names" in plural


def test_opening_a_result_records_history(window, client, qtbot, fake_os):
    f = window.bar.field
    f.setFocus()
    QTest.keyClicks(f, "gifttop")
    qtbot.waitUntil(lambda: window._displayed_key == window._req_key and window._last_result is not None, timeout=1000)
    assert not client.last.commit_history
    window.results.select_row(0)
    window.open_selected()
    assert client.user_ops[-1][0] == "record_open" and client.user_ops[-1][2] == "gifttop"


def test_idle_query_commits_history_and_keeps_selection(window, client, qtbot, monkeypatch):
    import fast_file_search.ui.main_window as mw

    window._idle_commit.setInterval(50)
    f = window.bar.field
    f.setFocus()
    QTest.keyClicks(f, "gifttop")
    qtbot.waitUntil(lambda: bool(client.searches) and client.last.text == "gifttop", timeout=1000)
    wait_result(qtbot, window)
    window.results.select_row(2)
    chosen = window.results.current_row().id
    qtbot.waitUntil(lambda: client.last.commit_history, timeout=1000)
    wait_result(qtbot, window)
    assert window.results.current_row().id == chosen          # same query: applied as a refresh


# ---------------------------------------------------------------- states (F.9)
def test_state_loading_then_queued_query_runs_on_ready(make_window, dataset, qtbot):
    client = FakeEngineClient(dataset, ready=False)
    win = make_window(client)
    assert win.results.empty_state.kind == "loading"
    assert win.status_lbl.text() == "Loading index…"
    f = win.bar.field
    assert f.isEnabled()
    f.setFocus()
    QTest.keyClicks(f, "invoice")
    QTest.keyClick(f, Qt.Key.Key_Return)
    assert client.searches == []                              # queued until Ready
    client.make_ready()
    qtbot.waitUntil(lambda: bool(client.searches), timeout=1000)
    assert client.last.text == "invoice" and client.last.commit_history
    wait_result(qtbot, win)
    assert win.results.is_showing_results()
    assert win.status_lbl.text() == "Index active"


def test_state_no_folders(make_window, qtbot, tmp_path, monkeypatch):
    from fast_file_search.engine.protocol import IndexStatus
    from fake_engine import FakeDataset

    client = FakeEngineClient(FakeDataset([]))
    win = make_window(client, paths=[])
    es = win.results.empty_state
    assert es.kind == "no-folders"
    assert [b.text() for b in es.buttons] == ["Add folder…", "Index your home folder"]
    from PySide6.QtWidgets import QFileDialog

    picked = tmp_path / "picked"
    picked.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(picked)))
    es.buttons[0].click()
    assert win.cm.config.index_paths == [os.path.normpath(str(picked))]
    assert client.configs[-1].roots == (os.path.normpath(str(picked)),)


def test_state_first_scan_and_ready(make_window, dataset, qtbot):
    client = FakeEngineClient(dataset, ready=False)
    win = make_window(client)
    client.make_ready(dataset.status(state="scanning", root_state={3: "scanning"}, scanned=12345))
    qtbot.waitUntil(lambda: win.results.empty_state.kind == "first-scan", timeout=1000)
    assert win.results.empty_state.title.text() == "Indexing Documents…"
    assert "12,345 items so far" in win.results.empty_state.message.text()
    assert win.status_lbl.text().startswith("Indexing Documents…")
    assert win.progress_bar.isVisible()
    client.push_status(dataset.status())
    assert win.results.empty_state.kind == "ready"
    assert "indexed" in win.results.empty_state.message.text() and "ext:pdf" in win.results.empty_state.message.text()
    assert not win.progress_bar.isVisible()


def test_state_no_matches_with_filters_and_did_you_mean(window, client, qtbot):
    window.chips._update(type_key="image", root_id=2)
    search(qtbot, window, "zzqx")
    es = window.results.empty_state
    assert es.kind == "no-matches" and "zzqx" in es.title.text()
    assert "Filters are on: Type: Images" in es.message.text()
    assert [b.text() for b in es.buttons] == ["Clear filters", "Search all folders"]
    es.buttons[1].click()
    assert not client.last.filters.root_ids and client.last.filters.kinds == frozenset({"image"})
    wait_result(qtbot, window)
    window.results.empty_state.buttons[0].click()
    assert not window.chips.is_active()

    client.responder = lambda req: SearchResult(gen=req.gen, total=0, offset=0, rows=(), elapsed_ms=1,
                                                did_you_mean="invoice")
    search(qtbot, window, "invocie")
    assert "invoice" in window.results.empty_state.link.text()
    client.responder = None
    window.results.empty_state.link.linkActivated.emit("invoice")
    assert window.bar.text() == "invoice" and client.last.text == "invoice"


def test_state_query_error_in_header_keeps_previous_list(window, client, qtbot):
    search(qtbot, window, "gifttopicon")
    n = window.results.model.rowCount()
    client.responder = lambda req: SearchResult(gen=req.gen, total=0, offset=0, rows=(), elapsed_ms=0,
                                                error="Not a valid regular expression: missing )")
    f = window.bar.field
    f.selectAll()
    QTest.keyClicks(f, "re:(")
    QTest.keyClick(f, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: "regular expression" in window.results.summary.text(), timeout=1000)
    assert "regular expression" in window.results.summary.text()
    assert window.results.summary.palette().color(window.results.summary.foregroundRole()) == theme.error_text_color()
    assert window.results.model.rowCount() == n and window.results.is_showing_results()


def test_engine_restarting_and_failed_notices(window, client):
    client.engine_state.emit("restarting")
    assert window.notice_bar.isVisible() and "Restarting" in window.notice_bar.text.text()
    client.engine_state.emit("failed")
    assert [b.text() for b in window.notice_bar.buttons] == ["Restart", "Show log folder"]
    assert window.status_lbl.text() == "Indexer stopped"
    window.notice_bar.buttons[0].click()
    assert client.started == 1
    client.engine_state.emit("ready")
    assert not window.notice_bar.has("engine")


def test_engine_notices_from_config(window, client):
    client.notice.emit(Notice("warning", "config_recovered", "The settings file was damaged and was reset."))
    assert window.notice_bar.isVisible() and "damaged" in window.notice_bar.text.text()
    assert window.notice_bar.buttons[0].text().startswith("Open")
    window.notice_bar.dismiss_btn.click()
    assert not window.notice_bar.isVisible()
    client.notice.emit(Notice("warning", "config_recovered", "again"))
    assert not window.notice_bar.isVisible()          # dismissed for this session


# ---------------------------------------------------------------- status bar (F.7)
def test_status_bar_texts(window, client, dataset):
    st = dataset.status()
    assert window.status_lbl.text() == "Index active"
    assert window.count_lbl.text() == f"{st.total_entries:,} items indexed"
    assert window.live_lbl.text() == "Live monitoring"
    client.push_status(dataset.status(live="limited"))
    assert window.live_lbl.text() == "Live monitoring limited" and "inotify" in window.live_lbl.toolTip()
    client.push_status(dataset.status(live="off", state="paused"))
    assert window.live_lbl.text() == "Live monitoring off"
    assert window.status_lbl.text() == "Index paused (on battery)"
    assert window.status_dot.state == "paused"


# ---------------------------------------------------------------- accessibility & rules
def test_every_interactive_widget_has_an_accessible_name(window, qtbot):
    search(qtbot, window, "gifttopicon")
    missing = []
    for w in window.findChildren(QWidget):
        if window.preview.isAncestorOf(w) or not w.isVisibleTo(window):
            continue
        if isinstance(w, QScrollBar) or w.objectName().startswith("qt_") or type(w).__name__.startswith("QLineEditIcon"):
            continue
        if isinstance(w, (QAbstractButton, QLineEdit, QAbstractItemView, QComboBox, QAbstractSpinBox, QTabBar)):
            if not w.accessibleName() and not (isinstance(w, QAbstractButton) and w.text()):
                missing.append((type(w).__name__, w.objectName(), type(w.parentWidget()).__name__, w.parentWidget().objectName()))
    assert missing == []
    assert window.results.view.accessibleName() == "Search results"
    assert window.bar.field.accessibleName() == "Search"


def test_no_sqlite_in_gui_modules():
    offenders = []
    for path in sorted(UI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            if any(n and (n.split(".")[0] == "sqlite3" or n.endswith(("persistent_index", "search_history")))
                   for n in names):
                offenders.append(path.name)
    assert offenders == []


def test_no_thread_per_keystroke_or_selection(window, qtbot):
    search(qtbot, window, "gift")
    threads_before = len(window.findChildren(QThread))
    py_before = threading.active_count()
    f = window.bar.field
    for ch in "topicon":
        QTest.keyClicks(f, ch)
        qtbot.wait(5)
    wait_result(qtbot, window, timeout=2000)
    for i in range(20):
        window.results.select_row(i % max(1, window.results.model.rowCount()))
    QApplication.processEvents()
    assert len(window.findChildren(QThread)) == threads_before
    assert threading.active_count() <= py_before + 1


def test_layout_state_is_remembered(make_window, client):
    win = make_window(client)
    win.resize(1000, 640)
    win.main_splitter.setSizes([200, 500, 300])
    win._set_preview_visible(False)
    win.close()
    assert theme.ui_settings().value("window/geometry_v2") is not None
    assert theme.ui_settings().value("results/header_v3") is not None
    win2 = make_window(client)
    assert not win2.act_preview.isChecked() and win2.preview.isHidden()
    assert win2.width() <= 1000


def test_close_quits_without_tray(window):
    from PySide6.QtGui import QCloseEvent

    window.tray_icon = None
    ev = QCloseEvent()
    window.closeEvent(ev)
    assert ev.isAccepted() and window._shutdown_done


def test_appearance_switch_updates_palette(window):
    try:
        window.set_appearance("dark")
        assert theme.is_dark() and window.appearance_actions["dark"].isChecked()
        window.set_appearance("light")
        assert not theme.is_dark()
    finally:
        window.set_appearance("system")


# ---------------------------------------------------------------- folders, settings, onboarding
def test_onboarding_on_first_run_configures_roots(make_window, qtbot, tmp_path, monkeypatch):
    from fake_engine import FakeDataset
    from fast_file_search.ui import onboarding

    a, b = tmp_path / "Desktop", tmp_path / "Documents"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(onboarding, "default_roots", lambda: [str(a), str(b)])
    client = FakeEngineClient(FakeDataset([]))
    win = make_window(client, paths=[], first_run=True)
    qtbot.waitUntil(lambda: getattr(win, "onboarding", None) is not None, timeout=1000)
    dlg = win.onboarding
    assert [x.text() for x in dlg.boxes] == ["Desktop", "Documents"]
    dlg.boxes[1].setChecked(False)
    dlg.start_btn.click()
    assert win.cm.config.index_paths == [os.path.normpath(str(a))]
    assert client.configs[-1].roots == (os.path.normpath(str(a)),)


def test_settings_changes_reconfigure_the_engine(window, client, monkeypatch):
    from fast_file_search.ui import main_window as mw

    class FakeDialog:
        def __init__(self, cm, parent=None, **kw):
            self.cm, self.appearance, self.indexing_changed = cm, "system", True
            self.kw = kw

        def exec(self):
            self.cm.config.live_monitoring = False
            return 1

    monkeypatch.setattr(mw, "SettingsDialog", FakeDialog)
    window.open_settings()
    assert client.configs[-1].live_monitoring is False


def test_update_and_rebuild_index(window, client, monkeypatch):
    window.act_update_index.trigger()
    assert client.index_calls[-1] == ("update", None)
    monkeypatch.setattr(window, "_confirm", lambda *a, **k: True)
    window.act_rebuild_index.trigger()
    assert client.index_calls[-1] == ("rebuild", None)


def test_preview_is_wired(window, client, qtbot, monkeypatch):
    search(qtbot, window, "gifttopicon")
    shown = []
    monkeypatch.setattr(window.preview, "show_row", lambda row, root: shown.append((row, root)))
    window.results.select_row(1)
    row = window.results.model.row(1)
    assert shown[-1][0] == row and shown[-1][1] == client.dataset.roots[row.root_id]
    sub = os.path.join(client.dataset.roots[1], "icons")
    window.preview.scope_requested.emit(sub)
    assert client.last.filters.folder == sub


def test_export_results(window, qtbot, tmp_path, monkeypatch):
    import json
    from PySide6.QtWidgets import QFileDialog

    search(qtbot, window, "gifttopicon")
    target = tmp_path / "out.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "JSON (*.json)")))
    window.export_results()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data) == window.results.model.rowCount() and {"name", "path", "size"} <= set(data[0])


def test_copy_diagnostics(window):
    window.copy_diagnostics()
    text = QGuiApplication.clipboard().text()
    assert "Fast File Search Pro" in text and "Index:" in text
