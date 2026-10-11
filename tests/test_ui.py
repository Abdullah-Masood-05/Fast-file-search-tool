"""Main window: layout, keyboard flow, context menu actions, empty states, lifetime.

OS actions (open, reveal, trash) are replaced with fakes; config.json and QSettings
go to a temp folder.
"""
import os
import sys
import time

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QAction, QGuiApplication, QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _wait(qapp, pred, timeout=15):
    deadline = time.time() + timeout
    while not pred() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    return pred()


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    from fast_file_search.config import AppConfig, config_manager

    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setenv("FAST_FILE_SEARCH_CACHE_DIR", str(tmp_path / "cache"))
    cfg = AppConfig()
    cfg.index_paths = []
    monkeypatch.setattr(config_manager, "config", cfg)
    monkeypatch.setattr(config_manager, "save", lambda: None)
    return cfg


@pytest.fixture
def sample(tmp_path):
    root = tmp_path / "Projects"
    (root / "Reports").mkdir(parents=True)
    (root / "Reports" / "report_notes.txt").write_text("quarterly report")
    (root / "Reports" / "quarterly_report.xlsx").write_bytes(b"x" * 2048)
    (root / "invoice.pdf").write_bytes(b"%PDF")
    return root


def _make_window(qapp, env, folders=()):
    from fast_file_search.ui.main_window import MainWindow

    env.index_paths = [str(f) for f in folders]
    win = MainWindow()
    win.show()
    assert _wait(qapp, lambda: not win.is_indexing())
    return win


@pytest.fixture
def window(qapp, env, sample):
    win = _make_window(qapp, env, [sample])
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


@pytest.fixture
def fake_os(monkeypatch):
    from fast_file_search.ui import file_actions

    calls = []
    monkeypatch.setattr(file_actions, "open_path", lambda p: calls.append(("open", p)))
    monkeypatch.setattr(file_actions, "reveal", lambda p: calls.append(("reveal", p)))
    monkeypatch.setattr(file_actions, "open_with", lambda p, a=None: calls.append(("open_with", p)))
    monkeypatch.setattr(file_actions, "move_to_trash", lambda p: (calls.append(("trash", p)), (True, None))[1])
    monkeypatch.setattr(file_actions, "delete_permanently", lambda p: calls.append(("delete", p)))
    return calls


def _search(qapp, win, text):
    sv = win.search_view
    sv.search_input.setFocus()
    sv.search_input.clear()
    QTest.keyClicks(sv.search_input, text)
    QTest.keyClick(sv.search_input, Qt.Key.Key_Return)
    _wait(qapp, lambda: win.search_worker is None or not win.search_worker.isRunning())
    _wait(qapp, lambda: sv.is_showing_results() or sv.empty_state.kind in ("no-matches", "error"))


# ---------------------------------------------------------------- structure

def test_native_window_with_menus_and_roles(window):
    win = window
    assert not win.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not hasattr(win, "title_bar")
    assert win.windowTitle() == "Fast File Search Pro"
    menus = [a.text().replace("&", "") for a in win.menuBar().actions()]
    assert menus == ["File", "Edit", "View", "Index", "Help"]
    assert win.act_settings.menuRole() == QAction.MenuRole.PreferencesRole
    assert win.act_quit.menuRole() == QAction.MenuRole.QuitRole
    assert win.act_about.menuRole() == QAction.MenuRole.AboutRole
    assert win.browser_view is not None and win.filter_panel is not None
    assert win.preview_panel is not None


def test_standard_shortcuts(window):
    win = window
    assert QKeySequence(QKeySequence.StandardKey.Find) in win.act_find.shortcuts()
    assert QKeySequence("Ctrl+L") in win.act_find.shortcuts()
    assert QKeySequence(QKeySequence.StandardKey.Copy) in win.act_copy_path.shortcuts()
    assert QKeySequence(QKeySequence.StandardKey.Open) in win.act_open.shortcuts()
    assert QKeySequence("Ctrl+Return") in win.act_reveal.shortcuts()


def test_focus_starts_in_search(window):
    assert window.focusWidget() is window.search_view.search_input


def test_icon_only_and_toggle_buttons_have_accessible_names(window):
    bar = window.browser_view.breadcrumb_bar
    assert bar.up_btn.accessibleName()
    assert window.search_view.fav_btn.accessibleName()
    assert window.filters_btn.accessibleName()
    assert window.search_view.results_view.accessibleName() == "Search results"


# ---------------------------------------------------------------- keyboard flow

def test_keyboard_flow_search_results_open_reveal(qapp, window, fake_os):
    win = window
    sv = win.search_view
    _search(qapp, win, "report")
    assert sv.result_count() >= 2
    assert "result" in win.status_lbl.text() and " in " in win.status_lbl.text()

    QTest.keyClick(sv.search_input, Qt.Key.Key_Down)          # Down moves into the results
    assert win.focusWidget() is sv.results_view
    assert sv.results_view.currentIndex().row() == 0
    first = sv.current_path()

    QTest.keyClick(sv.results_view, Qt.Key.Key_Return)         # Enter opens
    assert fake_os[-1] == ("open", first)
    QTest.keyClick(sv.results_view, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert fake_os[-1] == ("reveal", first)                    # Ctrl/Cmd+Enter reveals

    QTest.keyClick(sv.results_view, Qt.Key.Key_Up)             # Up on the first row: back to search
    assert win.focusWidget() is sv.search_input


def test_escape_clears_then_hides(qapp, window):
    win = window
    sv = win.search_view
    _search(qapp, win, "report")
    QTest.keyClick(sv.search_input, Qt.Key.Key_Escape)
    assert sv.search_input.text() == ""
    assert sv.result_count() == 0 and sv.empty_state.kind == "ready"
    hidden = []
    sv.hide_requested.connect(lambda: hidden.append(1))
    QTest.keyClick(sv.search_input, Qt.Key.Key_Escape)
    assert hidden == [1]
    qapp.processEvents()
    assert win.isMinimized() or not win.isVisible()


def test_escape_in_results_returns_to_search(qapp, window):
    win = window
    sv = win.search_view
    _search(qapp, win, "report")
    sv.focus_results()
    QTest.keyClick(sv.results_view, Qt.Key.Key_Escape)
    assert win.focusWidget() is sv.search_input
    assert sv.search_input.text() == "report"      # Esc in the list does not clear


def test_find_action_focuses_and_selects_search(qapp, window):
    win = window
    sv = win.search_view
    sv.search_input.setText("abc")
    sv.results_view.setFocus()
    win.act_find.trigger()
    assert win.focusWidget() is sv.search_input
    assert sv.search_input.selectedText() == "abc"


def test_item_actions_follow_selection(qapp, window, fake_os):
    win = window
    sv = win.search_view
    assert not win.act_open.isEnabled() and not win.act_trash.isEnabled()
    _search(qapp, win, "report")
    sv.focus_results()
    qapp.processEvents()
    assert win.act_open.isEnabled() and win.act_reveal.isEnabled()
    win.act_copy_path.trigger()
    assert QGuiApplication.clipboard().text() == sv.current_path()


# ---------------------------------------------------------------- context menu

def _menu_action(menu, text):
    for a in menu.actions():
        if a.text() == text:
            return a
    raise AssertionError(f"{text!r} not in {[a.text() for a in menu.actions()]}")


def test_context_menu_actions(qapp, window, fake_os, monkeypatch):
    from fast_file_search.ui import file_actions, theme

    win = window
    sv = win.search_view
    _search(qapp, win, "report")
    sv.focus_results()
    path = sv.current_path()
    menu = sv.build_context_menu([path])
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    for expected in ("Open", theme.reveal_label(), "Copy path", "Copy name", theme.trash_label()):
        assert expected in texts
    if file_actions.open_with_mode() in ("system", "choose-app"):
        assert "Open with…" in texts

    _menu_action(menu, "Open").trigger()
    assert fake_os[-1] == ("open", path)
    _menu_action(menu, theme.reveal_label()).trigger()
    assert fake_os[-1] == ("reveal", path)
    _menu_action(menu, "Copy path").trigger()
    assert QGuiApplication.clipboard().text() == path
    _menu_action(menu, "Copy name").trigger()
    assert QGuiApplication.clipboard().text() == os.path.basename(path)

    _menu_action(menu, "Add to favorites").trigger()
    assert sv.history_mgr.is_favorite(path)
    assert "Remove from favorites" in [a.text() for a in sv.build_context_menu([path]).actions()]

    # Trash: cancelled confirmation does nothing
    monkeypatch.setattr(sv, "_confirm", lambda *a, **k: False)
    _menu_action(menu, theme.trash_label()).trigger()
    assert ("trash", path) not in fake_os
    # Confirmed: moved to the trash and removed from the list
    monkeypatch.setattr(sv, "_confirm", lambda *a, **k: True)
    before = sv.result_count()
    _menu_action(menu, theme.trash_label()).trigger()
    assert fake_os[-1] == ("trash", path)
    assert sv.result_count() == before - 1 and sv.model.row_of(path) < 0


def test_multi_selection_menu_uses_plural_actions(qapp, window):
    win = window
    sv = win.search_view
    _search(qapp, win, "report")
    sv.results_view.selectAll()
    paths = sv.selected_paths()
    assert len(paths) >= 2
    texts = [a.text() for a in sv.build_context_menu(paths).actions()]
    assert "Copy paths" in texts and "Copy names" in texts


def test_trash_unavailable_offers_permanent_delete_only_after_confirmation(qapp, window, monkeypatch):
    from fast_file_search.ui import file_actions

    sv = window.search_view
    _search(qapp, window, "invoice")
    sv.focus_results()
    path = sv.current_path()
    calls = []
    monkeypatch.setattr(file_actions, "move_to_trash", lambda p: (False, None))   # no trash here
    monkeypatch.setattr(file_actions, "delete_permanently", lambda p: calls.append(p))
    answers = iter([True, False])           # yes to "move to trash", no to "delete permanently"
    asked = []
    monkeypatch.setattr(sv, "_confirm", lambda *a, **k: (asked.append(a[0]), next(answers))[1])
    sv.trash_paths([path])
    assert calls == [] and asked == [asked[0], "Delete permanently"]
    assert sv.model.row_of(path) >= 0
    answers = iter([True, True])
    sv.trash_paths([path])
    assert calls == [path] and sv.model.row_of(path) < 0


def test_os_errors_are_shown_in_plain_language(qapp, window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    from fast_file_search.ui import file_actions

    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: shown.append(text))
    window.search_view.open_selected(["/definitely/not/here.txt"])
    assert shown and "no longer exists" in shown[0]
    assert file_actions.reveal("/definitely/not/here.txt").startswith('"here.txt" no longer exists')


# ---------------------------------------------------------------- empty states

def test_empty_state_no_folders(qapp, env, monkeypatch, tmp_path):
    win = _make_window(qapp, env, [])
    try:
        es = win.search_view.empty_state
        assert es.kind == "no-folders"
        assert [b.text() for b in es.buttons] == ["Add folder…", "Index home folder"]
        picked = tmp_path / "picked"
        picked.mkdir()
        from PyQt6.QtWidgets import QFileDialog
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(picked)))
        es.buttons[0].click()
        assert env.index_paths == [os.path.normpath(str(picked))]
        assert _wait(qapp, lambda: not win.is_indexing())
        assert win.search_view.empty_state.kind == "ready"
    finally:
        win.close()


def test_empty_state_ready_and_indexing(qapp, window, monkeypatch):
    win = window
    es = win.search_view.empty_state
    assert es.kind == "ready"
    assert "indexed" in es.message.text()
    assert "items indexed" in win.index_lbl.text()
    monkeypatch.setattr(win, "is_indexing", lambda: True)
    win._indexed_count = 0
    win._refresh_empty_state()
    assert es.kind == "indexing" and es.title.text() == "Indexing…"


def test_empty_state_no_matches_with_filters(qapp, window):
    win = window
    sv = win.search_view
    _search(qapp, win, "zzqxnomatch")
    assert sv.empty_state.kind == "no-matches"
    assert "zzqxnomatch" in sv.empty_state.title.text()
    assert sv.empty_state.buttons == []

    win.filter_panel.size_combo.setCurrentIndex(1)
    win.filter_panel._filter_timer.stop()
    _search(qapp, win, "zzqxnomatch")
    assert [b.text() for b in sv.empty_state.buttons] == ["Reset filters"]
    sv.empty_state.buttons[0].click()
    assert win.filter_panel.active_filter_count() == 0


def test_search_error_is_shown(qapp, window):
    win = window
    win._on_search_failed("database is locked")
    assert win.search_view.empty_state.kind == "error"
    assert "database is locked" in win.search_view.empty_state.message.text()


# ---------------------------------------------------------------- lifetime & state

def test_layout_state_is_remembered(qapp, env, sample):
    from fast_file_search.ui import theme

    win = _make_window(qapp, env, [sample])
    win.resize(900, 640)
    win.toggle_filter_panel(False)
    win._set_preview_visible(False)
    win.close()
    assert theme.ui_settings().value("window/geometry") is not None
    assert theme.ui_settings().value("results/header") is not None

    win2 = _make_window(qapp, env, [sample])
    try:
        assert not win2.act_filters.isChecked() and win2.filter_panel.isHidden()
        assert not win2.act_preview.isChecked() and win2.preview_panel.isHidden()
        assert win2.width() <= 900   # restored (clamped to the 800x600 offscreen screen), not the 1180 default
    finally:
        win2.close()


def test_close_quits_without_tray(qapp, window, monkeypatch):
    from PyQt6.QtGui import QCloseEvent

    window.tray_icon = None
    ev = QCloseEvent()
    window.closeEvent(ev)
    assert ev.isAccepted() and window._shutdown_done


def test_appearance_switch_updates_palette(qapp, window):
    from fast_file_search.ui import theme

    try:
        window.set_appearance("dark")
        assert theme.is_dark()
        assert window.appearance_actions["dark"].isChecked()
        window.set_appearance("light")
        assert not theme.is_dark()
    finally:
        window.set_appearance("system")
