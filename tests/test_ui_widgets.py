"""Widget-level UI tests (moved from test_search_extra.py and updated for the
model/view results list, the location bar and the new filter controls)."""
import os
import sys
import time

import pytest
from PySide6.QtCore import QDate, QSettings, Qt
from PySide6.QtWidgets import QApplication

from fast_file_search.persistent_index import PersistentIndex
from fast_file_search.search import SearchResponse, SearchResult
from fast_file_search.search_history import SearchHistory


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Keep QSettings and config.json writes out of the user's real profile."""
    from fast_file_search.config import AppConfig, config_manager

    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setattr(config_manager, "config", AppConfig())
    monkeypatch.setattr(config_manager, "save", lambda: None)


def _file(path, size=1, is_folder=False):
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return {"path": path, "name": name, "extension": os.path.splitext(name)[1].lower(),
            "size": size, "modified": 1_700_000_000, "created": 1_700_000_000,
            "is_folder": is_folder}


@pytest.fixture
def search_view(qapp, tmp_path):
    from fast_file_search.ui.search_view import SearchView

    index = PersistentIndex(db_path=tmp_path / "i.db")
    history = SearchHistory(db_path=tmp_path / "h.db")
    view = SearchView(history_mgr=history, persistent_index=index)
    yield view
    view.shutdown()
    history.close()
    index.close()


def _results(n, prefix="f"):
    return [
        SearchResult(path=f"/p/{prefix}{i}.txt", name=f"{prefix}{i}.txt", extension=".txt",
                     size=i, modified=1_700_000_000 + i, is_folder=False, score=float(i))
        for i in range(n)
    ]


def _names(view):
    m = view.model
    return [m.result(r).name for r in range(m.rowCount())]


# ---------------------------------------------------------------- results list

def test_results_sorted_by_relevance_and_favorites_filter(search_view):
    from fast_file_search.ui.search_view import FavoriteRole

    view = search_view
    view.history_mgr.add_favorite("/p/f2.txt")
    view.set_results(SearchResponse(results=_results(5), total_count=5))
    assert view.result_count() == 5
    assert _names(view)[0] == "f4.txt"
    row = view.model.row_of("/p/f2.txt")
    assert view.model.index(row, 0).data(FavoriteRole) is True
    # the favourite is marked without an emoji in the name
    assert view.model.index(row, 0).data() == "f2.txt"

    view.fav_btn.setChecked(True)
    # a new result set must respect the active favourites filter
    view.set_results(SearchResponse(results=_results(5), total_count=5))
    assert _names(view) == ["f2.txt"]
    view.fav_btn.setChecked(False)
    assert view.result_count() == 5


def test_favorites_filter_with_no_favorites_shows_empty_state(search_view):
    view = search_view
    view.set_results(SearchResponse(results=_results(3), total_count=3))
    view.fav_btn.setChecked(True)
    assert not view.is_showing_results()
    assert view.empty_state.kind == "no-favorites"
    view.empty_state.buttons[0].click()   # "Show all results"
    assert view.is_showing_results() and view.result_count() == 3


def test_many_results_are_fast(search_view):
    view = search_view
    start = time.perf_counter()
    view.set_results(SearchResponse(results=_results(20000), total_count=20000))
    elapsed = time.perf_counter() - start
    assert view.result_count() == 20000
    assert elapsed < 1.5


def test_name_sort_ignores_case_and_relevance_restores(search_view):
    view = search_view
    view.history_mgr.add_favorite("/p/zeta.txt")
    res = [
        SearchResult("/p/zeta.txt", "zeta.txt", ".txt", 1, 1, False, 3.0),
        SearchResult("/p/Alpha.txt", "Alpha.txt", ".txt", 1, 1, False, 1.0),
        SearchResult("/p/beta.txt", "beta.txt", ".txt", 1, 1, False, 2.0),
    ]
    view.set_results(SearchResponse(results=res, total_count=3))
    view.results_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert _names(view) == ["Alpha.txt", "beta.txt", "zeta.txt"]
    view.sort_by_relevance()
    assert _names(view) == ["zeta.txt", "beta.txt", "Alpha.txt"]


def test_size_column_sorts_numerically_and_folders_first(search_view):
    from fast_file_search.ui.search_view import SIZE

    view = search_view
    res = [
        SearchResult("/p/big", "big", "", 9_000_000, 1, False, 1.0),
        SearchResult("/p/dir", "dir", "", 0, 1, True, 1.0),
        SearchResult("/p/small", "small", "", 20, 1, False, 1.0),
        SearchResult("/p/mid", "mid", "", 1500, 1, False, 1.0),
    ]
    view.set_results(SearchResponse(results=res, total_count=4))
    view.results_view.sortByColumn(SIZE, Qt.SortOrder.AscendingOrder)
    assert _names(view) == ["dir", "small", "mid", "big"]


def test_selection_survives_sorting(search_view):
    view = search_view
    view.set_results(SearchResponse(results=_results(50), total_count=50))
    assert view.select_path("/p/f7.txt")
    view.results_view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    assert view.current_path() == "/p/f7.txt"


def test_cells_are_human_readable(search_view):
    from fast_file_search.ui.search_view import FOLDER, MODIFIED, SIZE

    view = search_view
    r = SearchResult("/p/sub/a\nb.txt", "a\nb.txt", ".txt", 5 * 1024 * 1024, 1_700_000_000, False, 1.0)
    view.set_results(SearchResponse(results=[r], total_count=1))
    m = view.model
    assert m.index(0, 0).data() == "a?b.txt"            # control chars never break a row
    assert m.index(0, SIZE).data() == "5.0 MB"
    assert m.index(0, SIZE).data(Qt.ItemDataRole.TextAlignmentRole) & Qt.AlignmentFlag.AlignRight
    assert "bytes" in m.index(0, SIZE).data(Qt.ItemDataRole.ToolTipRole)
    assert len(m.index(0, MODIFIED).data()) == len("2023-11-14 22:13")
    assert m.index(0, MODIFIED).data(Qt.ItemDataRole.ToolTipRole)   # full timestamp
    assert m.index(0, FOLDER).data().replace("\\", "/").endswith("/p/sub")
    assert m.index(0, 0).data(Qt.ItemDataRole.DecorationRole) is not None


def test_match_ranges_merge_and_ignore_case():
    from fast_file_search.ui.search_view import match_ranges

    assert match_ranges("Quarterly_Report_report.pdf", ["report"]) == [(10, 16), (17, 23)]
    assert match_ranges("abcdef", ["bc", "cd"]) == [(1, 4)]
    assert match_ranges("abc", []) == []


def test_drag_exports_file_urls(search_view):
    view = search_view
    view.set_results(SearchResponse(results=_results(3), total_count=3))
    mime = view.model.mimeData([view.model.index(0, 0), view.model.index(0, 1), view.model.index(1, 0)])
    assert len(mime.urls()) == 2 and all(u.isLocalFile() for u in mime.urls())


def test_did_you_mean_label_is_escaped_and_errors_shown(search_view):
    view = search_view
    view.set_results(SearchResponse(did_you_mean="it's <b>x</b>"))
    text = view.dym_lbl.text()
    assert "<b>x</b>" not in text and "&lt;b&gt;" in text
    view.set_results(SearchResponse(error="Invalid regular expression: boom"))
    assert "boom" in view.dym_lbl.text()
    view.set_results(SearchResponse())
    assert view.dym_lbl.isHidden()


def test_dym_click_does_not_reopen_suggestions(search_view):
    view = search_view
    triggered = []
    view.search_triggered.connect(triggered.append)
    view.apply_dym_correction("corrected")
    assert triggered == ["corrected"]
    assert not view.debounce_timer.isActive()


def test_suggestion_thread_delivers_after_idle_and_shuts_down(search_view, qapp):
    view = search_view
    view.autocomplete.index.add_file(_file("/a/zebra_file.txt"))
    got = []
    view.suggestion_thread.suggestions_ready.connect(lambda q, r: got.append((q, r)))
    for _ in range(5):          # repeatedly start/stop the worker
        got.clear()
        view.suggestion_thread.fetch("zebra")
        deadline = time.time() + 5
        while not got and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert got and got[0][1] == ["zebra_file.txt"]
    view.shutdown()
    assert not view.suggestion_thread.isRunning()
    view.suggestion_thread.fetch("zebra")      # ignored after shutdown, must not restart
    assert not view.suggestion_thread.isRunning()


def test_suggestion_is_applied_as_a_search(search_view):
    view = search_view
    triggered = []
    view.search_triggered.connect(triggered.append)
    view.apply_suggestion("report.txt")
    assert triggered == ["report.txt"]
    assert view.search_input.text() == "report.txt"


def test_suggestion_popup_keyboard(search_view, qapp):
    from PySide6.QtTest import QTest

    view = search_view
    view.show()
    view.activateWindow()
    QTest.qWaitForWindowActive(view, 2000)   # suggestions only pop up while the field has focus
    got = []
    view.search_triggered.connect(got.append)
    view.search_input.setFocus()
    QTest.keyClicks(view.search_input, "rep")
    view._on_suggestions_ready("rep", ["report.txt", "repo"])
    popup = view.completer.popup()
    assert popup.isVisible() and not popup.currentIndex().isValid()
    QTest.keyClick(popup, Qt.Key.Key_Return)               # nothing highlighted: typed text
    assert got == ["rep"] and not popup.isVisible()
    view._last_searched = ""
    view._on_suggestions_ready("rep", ["report.txt", "repo"])
    QTest.keyClick(popup, Qt.Key.Key_Down)
    QTest.keyClick(popup, Qt.Key.Key_Return)               # highlighted: that suggestion, once
    assert got == ["rep", "report.txt"]
    view.hide()


def test_trash_removes_from_index_and_export_list(search_view, tmp_path, monkeypatch):
    from fast_file_search.ui import file_actions

    victim = tmp_path / "victim.txt"
    victim.write_text("x")
    view = search_view
    path = str(victim).replace("\\", "/")
    view.index.add_file(_file(path))
    view.set_results(SearchResponse(results=[SearchResult(path, "victim.txt", ".txt", 1, 1, False, 1.0)],
                                    total_count=1))
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    calls = []

    def fake_trash(p):
        calls.append(p)
        os.remove(p)
        return True, None

    monkeypatch.setattr(file_actions, "move_to_trash", fake_trash)
    view.select_path(path)
    view.trash_paths()
    assert calls == [path]
    assert not victim.exists()
    assert view.result_count() == 0
    assert view.raw_results == []
    assert not view.index.file_exists(path)


# ---------------------------------------------------------------- browser / location bar

def test_location_navigation_does_not_trigger_indexing(qapp, tmp_path):
    from fast_file_search.ui.browser_view import BrowserView

    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    view = BrowserView()
    navigated, indexed = [], []
    view.path_changed.connect(navigated.append)
    view.select_folder.connect(indexed.append)
    view.set_root_path(str(sub))

    view.breadcrumb_bar.up_btn.click()               # parent folder
    assert navigated and os.path.normcase(navigated[-1]) == os.path.normcase(str(sub.parent))
    assert not indexed                               # navigation only; nothing indexed

    view.breadcrumb_bar.folder_chosen.emit(str(sub))  # explicit "Add folder…" does index
    assert indexed == [str(sub)]


def test_location_menu_lists_ancestors_and_recent(qapp, tmp_path):
    from fast_file_search.ui.browser_view import BreadcrumbBar

    bar = BreadcrumbBar()
    other = tmp_path / "other"
    other.mkdir()
    sub = tmp_path / "one" / "two"
    sub.mkdir(parents=True)
    bar.set_path(str(other))
    bar.set_path(str(sub))
    bar._fill_path_menu()
    targets = [a.data() for a in bar.path_menu.actions() if a.data()]
    assert os.path.normcase(targets[0]) == os.path.normcase(str(sub))          # current first
    assert os.path.normcase(targets[1]) == os.path.normcase(str(sub.parent))
    assert any(os.path.normcase(t) == os.path.normcase(str(other)) for t in targets)  # recent
    emitted = []
    bar.path_selected.connect(emitted.append)
    bar.path_menu.actions()[1].trigger()
    assert os.path.normcase(emitted[-1]) == os.path.normcase(str(sub.parent))


def test_location_recent_history_is_bounded(qapp, tmp_path):
    from fast_file_search.ui.browser_view import BreadcrumbBar

    bar = BreadcrumbBar()
    for i in range(40):
        d = tmp_path / f"d{i}"
        d.mkdir()
        bar.set_path(str(d))
    assert len(bar.recent_paths) == BreadcrumbBar.MAX_RECENT


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are Windows specific")
def test_ancestors_handle_unc_and_drive_root():
    from fast_file_search.ui.browser_view import ancestor_targets

    crumbs = ancestor_targets("\\\\server\\share\\folder")
    assert ("\\\\server\\share", "\\\\server\\share\\") in crumbs
    assert crumbs[-1] == ("folder", "\\\\server\\share\\folder")
    assert ancestor_targets("C:\\") == [("C:", "C:\\")]


def test_browser_accepts_folder_drops(qapp, tmp_path):
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDropEvent
    from fast_file_search.ui.browser_view import BrowserView

    view = BrowserView()
    assert view.acceptDrops() and not view.tree_view.acceptDrops()
    got = []
    view.folders_dropped.connect(got.append)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    ev = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(ev)
    assert got == [[os.path.normpath(str(tmp_path))]]
    f = tmp_path / "f.txt"
    f.write_text("x")
    mime2 = QMimeData()
    mime2.setUrls([QUrl.fromLocalFile(str(f))])
    ev2 = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime2,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(ev2)
    assert len(got) == 1   # files (not folders) are ignored


def test_indexing_status_ignores_blank_exclude_patterns(qapp, tmp_path, monkeypatch):
    from fast_file_search.config import config_manager
    from fast_file_search.ui.browser_view import BrowserView

    view = BrowserView()
    view.set_root_path(str(tmp_path))
    monkeypatch.setattr(config_manager.config, "exclude_patterns", [""])
    monkeypatch.setattr(config_manager.config, "index_paths", [str(tmp_path.parent)])
    idx = view.tree_view.rootIndex()
    assert idx.isValid()
    assert view.get_folder_indexing_status(idx) == "indexed"
    monkeypatch.setattr(config_manager.config, "index_paths", [str(tmp_path / "inner")])
    assert view.get_folder_indexing_status(idx) == "partial"
    monkeypatch.setattr(config_manager.config, "exclude_patterns", [tmp_path.name])
    assert view.get_folder_indexing_status(idx) == "excluded"


def test_browser_columns_preset_is_remembered(qapp):
    from fast_file_search.ui.browser_view import BrowserView

    view = BrowserView()
    assert view.current_preset == "Minimal" and view.tree_view.isHeaderHidden()
    view.apply_column_preset("Detailed")
    assert BrowserView().current_preset == "Detailed"


# ---------------------------------------------------------------- filters

def test_reset_filters_emits_once(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    panel.update_extensions([".pdf", ".txt"])
    panel.ext_combo.setCurrentIndex(1)
    panel.size_combo.setCurrentIndex(3)
    panel.regex_input.setText("abc")
    assert panel.active_filter_count() == 3 and panel.clear_filters_btn.isEnabled()
    panel._filter_timer.stop()
    count = []
    panel.filters_changed.connect(lambda: count.append(1))
    panel.reset_filters()
    qapp.processEvents()
    time.sleep(0.4)
    qapp.processEvents()
    assert len(count) == 1
    vals = panel.get_filter_values()
    assert vals["extension"] is None and vals["size_range"] is None and vals["regex"] is None
    assert not panel.clear_filters_btn.isEnabled()


def test_size_presets_map_to_byte_ranges(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    panel.size_combo.setCurrentIndex(1)
    assert panel.get_filter_values()["size_range"] == (0, 10 * 1024)
    panel.size_combo.setCurrentIndex(panel.size_combo.count() - 1)
    assert panel.get_filter_values()["size_range"] == (1024 ** 3, None)


def test_filter_changes_are_debounced(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    count = []
    panel.filters_changed.connect(lambda: count.append(1))
    for ch in "hello":
        panel.regex_input.setText(panel.regex_input.text() + ch)
    assert count == []
    deadline = time.time() + 3
    while not count and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    time.sleep(0.4)
    qapp.processEvents()
    assert len(count) == 1


def test_invalid_regex_filter_is_ignored_and_explained(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    panel.regex_input.setText("([")
    assert panel.get_filter_values()["regex"] is None
    assert not panel.regex_error.isHidden()
    assert "regular expression" in panel.regex_error.text()
    panel.regex_input.setText(r"src\\")
    assert panel.get_filter_values()["regex"] == r"src\\"
    assert panel.regex_error.isHidden()


def test_update_extensions_preserves_selection(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    panel.update_extensions([".pdf", ".txt"])
    panel.ext_combo.setCurrentIndex(panel.ext_combo.findData(".txt"))
    panel.update_extensions([".txt", ".md"])
    assert panel.get_filter_values()["extension"] == ".txt"
    panel.update_extensions([".md"])      # selection vanished from the top list: still kept
    assert panel.get_filter_values()["extension"] == ".txt"


def test_date_presets_use_calendar_days(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    midnight = QDate.currentDate().startOfDay().toSecsSinceEpoch()
    panel.date_combo.setCurrentIndex(panel.date_combo.findData("today"))
    lo, hi = panel.get_filter_values()["date_range"]
    assert lo == midnight and hi >= lo
    panel.date_combo.setCurrentIndex(panel.date_combo.findData("yesterday"))
    lo, hi = panel.get_filter_values()["date_range"]
    assert hi == midnight - 1 and lo == QDate.currentDate().addDays(-1).startOfDay().toSecsSinceEpoch()
    panel.date_combo.setCurrentIndex(panel.date_combo.findData("custom"))
    assert not panel.custom_date_widget.isHidden()
    panel.start_date_edit.setDate(QDate(2024, 5, 10))
    panel.end_date_edit.setDate(QDate(2024, 5, 1))   # reversed on purpose
    lo, hi = panel.get_filter_values()["date_range"]
    assert lo < hi


# ---------------------------------------------------------------- preview

def _wait_for(qapp, pred, timeout=10):
    deadline = time.time() + timeout
    while not pred() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert pred()


def test_preview_panel_plain_text_single_highlighter_and_metadata(qapp, tmp_path):
    from fast_file_search.ui.widgets import PreviewPanel

    panel = PreviewPanel()
    html_file = tmp_path / "page.html"
    html_file.write_text("<html><body><b>needle</b> hay</body></html>", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("needle again", encoding="utf-8")
    panel.set_search_terms(["needle"])

    panel.load_file(str(html_file))
    assert panel.file_title.text() == "page.html"
    _wait_for(qapp, lambda: "needle" in panel.text_preview.toPlainText())
    assert "<b>needle</b>" in panel.text_preview.toPlainText()   # shown verbatim
    _wait_for(qapp, lambda: len(panel.meta_labels["MD5"].text()) == 32)
    assert len(panel.meta_labels["SHA-256"].text()) == 64
    assert panel.meta_labels["Modified"].text()[:2] == "20"     # real file time, not 1970
    assert "bytes" in panel.meta_labels["Size"].text()

    first = panel.highlighter
    assert first is not None
    panel.load_file(str(other))
    _wait_for(qapp, lambda: panel.text_preview.toPlainText() == "needle again")
    assert panel.highlighter is first          # reused, not stacked on the document
    panel.clear()
    assert panel.file_title.text() == "No file selected"
    panel.shutdown()


def test_preview_does_not_terminate_threads_and_ignores_stale(qapp, tmp_path):
    from fast_file_search.ui.widgets import PreviewPanel

    panel = PreviewPanel()
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(4 * 1024 * 1024))
    files = []
    for i in range(6):
        f = tmp_path / f"f{i}.txt"
        f.write_text(f"file number {i}")
        files.append(f)
    panel.load_file(str(big))
    for f in files:
        panel.load_file(str(f))             # rapid reselection cancels earlier loaders
    _wait_for(qapp, lambda: panel.text_preview.toPlainText() == "file number 5")
    panel.shutdown()
    assert not panel._retired_threads or all(not t.isRunning() for t in panel._retired_threads)


def test_preview_binary_decoding():
    from fast_file_search.ui.widgets import PreviewLoaderThread

    assert PreviewLoaderThread._decode_preview(b"MZ\x90\x00\x03\x00") is None
    assert "héllo" in PreviewLoaderThread._decode_preview("héllo".encode("utf-16"))
    assert PreviewLoaderThread._decode_preview("plain".encode()) == "plain"


def test_find_resource_locates_icon_and_misses_cleanly():
    from fast_file_search.ui.widgets import find_resource

    icon = find_resource("files.ico")
    assert icon is not None and icon.name == "files.ico"
    assert find_resource("definitely-not-bundled.xyz") is None


def test_highlighter_set_terms_rebuilds_rules(qapp):
    from PySide6.QtGui import QTextDocument
    from fast_file_search.ui.widgets import MatchHighlighter

    doc = QTextDocument()
    h = MatchHighlighter(doc, ["a.b", "x"])      # 1-char terms ignored; metacharacters escaped
    assert len(h.rules) == 1 and h.rules[0][0].search("a.b") and not h.rules[0][0].search("axb")
    h.set_terms([])
    assert h.rules == []


# ---------------------------------------------------------------- dialogs

def test_settings_dialog_detects_equivalent_paths(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from fast_file_search.ui.widgets import SettingsDialog

    dlg = SettingsDialog()
    dlg.paths_list.clear()
    forward = str(tmp_path).replace("\\", "/")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: forward))
    dlg.add_indexed_path()
    dlg.add_indexed_path()
    assert dlg.paths_list.count() == 1
    assert dlg.paths_list.item(0).text() == os.path.normpath(forward)
    if sys.platform in ("win32", "darwin"):
        monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: forward.upper()))
        dlg.add_indexed_path()
        assert dlg.paths_list.count() == 1


def test_settings_dialog_saves_and_reports_indexing_changes(qapp, tmp_path):
    from fast_file_search.config import config_manager
    from fast_file_search.ui import theme
    from fast_file_search.ui.widgets import SettingsDialog

    dlg = SettingsDialog()
    assert dlg.windowTitle() == theme.settings_label()
    dlg.max_results_spin.setValue(250)
    dlg.appearance_combo.setCurrentIndex(dlg.appearance_combo.findData("dark"))
    dlg.save_settings()
    assert config_manager.config.max_results == 250
    assert not dlg.indexing_changed
    assert theme.ui_settings().value("appearance") == "dark"

    dlg = SettingsDialog()
    dlg.paths_list.addItem(str(tmp_path))
    dlg.content_check.setChecked(True)
    dlg.save_settings()
    assert dlg.indexing_changed
    assert config_manager.config.index_paths == [str(tmp_path)]
    assert config_manager.config.index_content is True
    theme.ui_settings().remove("appearance")


def test_about_dialog_opens_and_shows_version(qapp):
    from fast_file_search.ui.widgets import AboutDialog, app_version

    dlg = AboutDialog()
    assert app_version() in dlg.version_lbl.text()


def test_export_writes_csv_and_json(qapp, tmp_path, monkeypatch):
    import json
    from PySide6.QtWidgets import QFileDialog
    from fast_file_search.ui.widgets import export_results

    rows = [r.to_dict() for r in _results(3)]
    target = tmp_path / "out.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "CSV (*.csv)")))
    assert "Exported 3 results" in export_results(None, rows)
    assert target.read_text(encoding="utf-8-sig").splitlines()[0].startswith("Name,")
    target = tmp_path / "out"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "JSON (*.json)")))
    export_results(None, rows)
    assert len(json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))) == 3
    assert export_results(None, []) == "There are no results to export."


def test_empty_state_replaces_buttons_without_leftovers(qapp):
    from PySide6.QtWidgets import QPushButton
    from fast_file_search.ui.widgets import EmptyState

    es = EmptyState()
    es.resize(500, 400)
    es.show()
    clicked = []
    es.set_content("a", "Title A", "Message", [("One", lambda: clicked.append(1)), ("Two", lambda: None)])
    es.set_content("b", "Title B", "", [("Three", lambda: clicked.append(3))])
    qapp.processEvents()
    visible = [b.text() for b in es.findChildren(QPushButton) if not b.isHidden()]
    assert visible == ["Three"]
    assert es.kind == "b" and es.message.isHidden()
    es.buttons[0].click()
    assert clicked == [3]


# ---------------------------------------------------------------- theme helpers

def test_secondary_text_meets_contrast_in_light_and_dark(qapp):
    from PySide6.QtGui import QPalette
    from fast_file_search.ui import theme

    label = theme.SecondaryLabel("x")
    try:
        for mode in ("light", "dark"):
            theme.apply_appearance(mode)
            qapp.processEvents()
            pal = QApplication.palette()
            fg = label.palette().color(QPalette.ColorRole.WindowText)
            assert theme.contrast_ratio(fg, pal.color(QPalette.ColorRole.Window)) >= 4.5
            assert theme.is_dark() == (mode == "dark")
    finally:
        theme.apply_appearance("system")


def test_formatting_helpers():
    from fast_file_search.ui import theme

    assert theme.format_size(512) == "512 B"
    assert theme.format_size(0) == "0 B"
    assert theme.format_date_short(0) == ""
    assert theme.display_text("a\tb\nc") == "a?b?c"
    assert theme.reveal_label() in ("Show in Finder", "Show in File Explorer", "Show in File Manager")
