"""Regression tests for the search engine, search history and the search/browse UI."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fast_file_search.persistent_index import PersistentIndex
from fast_file_search.search import (
    Levenshtein,
    QueryParser,
    SearchEngine,
    SearchResponse,
    SearchResult,
)
from fast_file_search.search_history import Autocomplete, SearchHistory


def _file(path, size=100, modified=1_700_000_000, is_folder=False):
    name = path.rsplit("/", 1)[-1]
    ext = "" if is_folder or "." not in name else "." + name.rsplit(".", 1)[-1].lower()
    return {
        "path": path,
        "name": name,
        "extension": ext,
        "size": size,
        "modified": modified,
        "created": modified,
        "is_folder": is_folder,
        "parent_path": path.rsplit("/", 1)[0],
    }


@pytest.fixture
def engine(tmp_path):
    index = PersistentIndex(db_path=tmp_path / "index.db")
    now = int(time.time())
    files = [
        _file("/docs/annual_report_2024.pdf", 2 * 1024 * 1024, 1_700_000_000),
        _file("/docs/draft_notes.txt", 5 * 1024, 1_710_000_000),
        _file("/docs/my docs/final_draft.txt", 50, 1_720_000_000),
        _file("/code/main.c", 300, now),
        _file("/code/main.cpp", 400, now),
        _file("/code/Readme.md", 1000, now),
        _file("/code/README.txt", 1000, now),
        _file("/code/future.py", 10, now + 10 * 24 * 3600),  # mtime in the future
        _file("/pics/photo_a.jpg", 3 * 1024 * 1024, 1_600_000_000),
        _file("/pics/photo_b.png", 4 * 1024 * 1024, 1_650_000_000),
        _file("/odd/50%_off_my_file.txt", 5, 1_700_000_000),
    ]
    index.add_files_batch(files)
    eng = SearchEngine(persistent_index=index)
    yield eng
    index.close()


def names(resp):
    return sorted(r.name for r in resp.results)


# ---------------------------------------------------------------- query parser

def test_parser_case_prefix_and_regex_prefix():
    q = QueryParser.parse("cs:Readme")
    assert q.case_sensitive and q.terms == ["Readme"]
    q = QueryParser.parse("case:Readme")
    assert q.case_sensitive and q.terms == ["Readme"]
    # "case:" appearing later in the query is not a case-sensitivity switch
    assert not QueryParser.parse("a case:b").case_sensitive
    q = QueryParser.parse("re:^main\\.c$")
    assert q.is_regex and q.terms == ["^main\\.c$"]


def test_parser_boolean_words_must_be_uppercase():
    q = QueryParser.parse("rock and roll")
    assert q.terms == ["rock", "and", "roll"] and not q.boolean_groups
    q = QueryParser.parse("a OR b")
    assert q.boolean_groups == [("OR", "b")]


def test_parser_field_word_boundary_and_quoted_values():
    q = QueryParser.parse("myname:foo")
    assert not q.field_filters and q.terms == ["myname:foo"]
    q = QueryParser.parse('path:"my docs" draft')
    assert q.field_filters["path"] == "my docs"
    assert q.terms == ["draft"] and q.exact_phrase is None


def test_parser_ranges_and_extra_quotes():
    q = QueryParser.parse("size:1mb..5mb")
    assert q.range_filters["size"] == ("1mb", "5mb")
    q = QueryParser.parse("size:<=5mb date:>=2024-01-01")
    assert q.range_filters["size"] == (None, "5mb")
    assert q.range_filters["date"] == ("2024-01-01", None)
    q = QueryParser.parse('"first one" "second one"')
    assert q.exact_phrase == "first one" and q.terms == ["second one"]
    # unbalanced quote does not poison terms
    assert QueryParser.parse('"foo').terms == ["foo"]


# ------------------------------------------------------------------- searching

def test_multiple_terms_are_anded(engine):
    resp = engine.search_persistent("draft notes", use_cache=False)
    assert names(resp) == ["draft_notes.txt"]


def test_or_and_not(engine):
    resp = engine.search_persistent("photo_a OR photo_b", use_cache=False)
    assert names(resp) == ["photo_a.jpg", "photo_b.png"]
    resp = engine.search_persistent("draft NOT final", use_cache=False)
    assert names(resp) == ["draft_notes.txt"]
    resp = engine.search_persistent("draft AND final", use_cache=False)
    assert names(resp) == ["final_draft.txt"]


def test_wildcards(engine):
    resp = engine.search_persistent("*.jpg", use_cache=False)
    assert names(resp) == ["photo_a.jpg"]
    resp = engine.search_persistent("photo_?.*", use_cache=False)
    assert names(resp) == ["photo_a.jpg", "photo_b.png"]


def test_extension_filter_is_exact_not_substring(engine):
    resp = engine.search_persistent("main ext:c", use_cache=False)
    assert names(resp) == ["main.c"]
    resp = engine.search_persistent("main ext:.cpp", use_cache=False)
    assert names(resp) == ["main.cpp"]
    resp = engine.search_persistent("photo ext:jpg,png", use_cache=False)
    assert names(resp) == ["photo_a.jpg", "photo_b.png"]
    resp = engine.search_persistent("main ext:!c", use_cache=False)
    assert names(resp) == ["main.cpp"]


def test_filter_only_queries(engine):
    resp = engine.search_persistent("ext:png", use_cache=False)
    assert names(resp) == ["photo_b.png"]
    resp = engine.search_persistent("size:>2mb", use_cache=False)
    assert names(resp) == ["annual_report_2024.pdf", "photo_a.jpg", "photo_b.png"]


def test_size_and_date_ranges(engine):
    resp = engine.search_persistent("photo size:3mb..3.5mb", use_cache=False)
    assert names(resp) == ["photo_a.jpg"]
    # "date" is an alias of modified; previously it matched nothing at all
    resp = engine.search_persistent("photo date:2020", use_cache=False)
    assert names(resp) == ["photo_a.jpg"]
    resp = engine.search_persistent("photo modified:2021-01-01..2022-12-31", use_cache=False)
    assert names(resp) == ["photo_b.png"]


def test_path_filter_accepts_forward_slashes_on_any_platform(engine):
    # stored paths may use either separator; the filter must not care
    engine.index.add_file(_file("/x/y/win_style.txt"))
    engine.index.add_file(
        {**_file("/x/y/win_style2.txt"), "path": "C:\\x\\y\\win_style2.txt", "parent_path": "C:\\x\\y"}
    )
    resp = engine.search_persistent("win_style path:x/y", use_cache=False)
    assert names(resp) == ["win_style.txt", "win_style2.txt"]


def test_quoted_path_filter_with_spaces(engine):
    resp = engine.search_persistent('draft path:"my docs"', use_cache=False)
    assert names(resp) == ["final_draft.txt"]


def test_case_sensitive_search(engine):
    assert names(engine.search_persistent("readme", use_cache=False)) == ["README.txt", "Readme.md"]
    assert names(engine.search_persistent("cs:Readme", use_cache=False)) == ["Readme.md"]
    assert names(engine.search_persistent("case:README", use_cache=False)) == ["README.txt"]


def test_regex_query(engine):
    resp = engine.search_persistent(r"re:^main\.(c|cpp)$", use_cache=False)
    assert names(resp) == ["main.c", "main.cpp"]
    resp = engine.search_persistent("re:photo_[ab]", use_cache=False)
    assert names(resp) == ["photo_a.jpg", "photo_b.png"]


def test_invalid_regex_reports_error_instead_of_raising(engine):
    resp = engine.search_persistent("re:([", use_cache=False)
    assert resp.total_count == 0
    assert resp.error and "regular expression" in resp.error


def test_future_modified_time_does_not_crash_ranking(engine):
    resp = engine.search_persistent("future", use_cache=False)
    assert names(resp) == ["future.py"]


def test_literal_percent_and_underscore(engine):
    resp = engine.search_persistent("50%_off", use_cache=False)
    assert names(resp) == ["50%_off_my_file.txt"]


def test_pagination_clamps_bad_values(engine):
    resp = engine.search_persistent("txt", page=0, page_size=0, use_cache=False)
    assert resp.page == 1 and resp.page_size == 1
    assert len(resp.results) == 1
    assert resp.total_count >= 3 and resp.total_pages == resp.total_count
    resp2 = engine.search_persistent("txt", page=2, page_size=1, use_cache=False)
    assert resp2.results and resp2.results[0].path != resp.results[0].path


def test_cache_returns_copies_so_post_filtering_does_not_corrupt_it(engine):
    first = engine.search_persistent("txt")
    count = first.total_count
    assert count >= 3
    first.results = []          # what a caller doing post-filtering does
    first.total_count = 0
    second = engine.search_persistent("txt")
    assert second.total_count == count and len(second.results) == count
    third = engine.search_persistent("txt")
    assert third is not second and third.results is not second.results


def test_cache_is_invalidated_when_index_changes(engine):
    assert engine.search_persistent("zebra").total_count == 0
    engine.index.add_file(_file("/z/zebra.txt"))
    assert engine.search_persistent("zebra").total_count == 1
    engine.index.remove_file("/z/zebra.txt")
    assert engine.search_persistent("zebra").total_count == 0


def test_did_you_mean_for_misspelling(engine):
    resp = engine.search_persistent("photo_c", use_cache=False)
    assert resp.total_count == 0
    assert resp.did_you_mean in ("photo_a", "photo_b")


def test_fuzzy_match_length_shortcut():
    assert not Levenshtein.fuzzy_match("a", "abcdefgh", 2)
    assert Levenshtein.fuzzy_match("report", "reprot", 2)


def test_only_requested_page_is_materialised(engine):
    resp = engine.search_persistent("txt", page=1, page_size=2, use_cache=False)
    assert len(resp.results) == 2 and resp.total_count >= 3


# --------------------------------------------------------------- search history

def test_history_dedupes_and_bounds_size(tmp_path):
    h = SearchHistory(db_path=tmp_path / "h.db")
    h.add_search("alpha", 1)
    h.add_search("beta", 2)
    h.add_search("alpha", 3)
    h.add_search("   ")      # ignored
    h.add_search("")         # ignored
    recent = h.recent()
    assert [e.query for e in recent] == ["alpha", "beta"]
    assert recent[0].result_count == 3
    assert dict(h.frequent_queries())["alpha"] == 2

    for i in range(250):
        h.add_search(f"q{i}", i)
    total = h._conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    assert total == SearchHistory.MAX_ENTRIES
    assert h.recent(1)[0].query == "q249"      # stable newest-first even with equal timestamps
    h.close()


def test_history_pinned_flag_and_persistence(tmp_path):
    path = tmp_path / "h.db"
    h = SearchHistory(db_path=path)
    h.add_search("keep me", 4)
    h.pin_search("keep me")
    assert h.recent()[0].pinned is True
    h.unpin_search("keep me")
    assert h.recent()[0].pinned is False
    h.close()
    h2 = SearchHistory(db_path=path)
    assert [e.query for e in h2.recent()] == ["keep me"]
    h2.close()


def test_history_recovers_from_corrupt_database(tmp_path):
    path = tmp_path / "h.db"
    path.write_bytes(b"this is definitely not an sqlite database" * 50)
    h = SearchHistory(db_path=path)          # must not raise
    h.add_search("works again", 1)
    assert [e.query for e in h.recent()] == ["works again"]
    h.close()
    assert (tmp_path / "h.db.corrupt").exists()


def test_history_falls_back_to_memory_when_location_unusable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    h = SearchHistory(db_path=blocker / "sub" / "h.db")   # parent is a file -> mkdir fails
    h.add_search("still ok", 1)
    assert h.recent()[0].query == "still ok"
    h.close()


def test_autocomplete_treats_like_wildcards_literally(tmp_path):
    index = PersistentIndex(db_path=tmp_path / "i.db")
    index.add_files_batch([_file("/a/my_file.txt"), _file("/a/myXfile.txt"), _file("/a/50%.txt")])
    ac = Autocomplete(persistent_index=index)
    assert ac.suggest("my_f") == ["my_file.txt"]
    assert ac.suggest("50%") == ["50%.txt"]
    assert ac.suggest("%") == ["50%.txt"]
    index.close()


def test_context_suggest_prefers_current_folder(tmp_path):
    index = PersistentIndex(db_path=tmp_path / "i.db")
    index.add_files_batch([_file("/a/one/report.txt"), _file("/b/two/report_final.txt"),
                           _file("/b/two/other.txt")])
    ac = Autocomplete(persistent_index=index)
    assert ac.context_suggest("report", "/b/two") == ["report_final.txt", "report.txt"]
    assert ac.context_suggest("report", "") [0] in ("report.txt", "report_final.txt")
    assert ac.context_suggest("", "/b") == []
    index.close()


# ------------------------------------------------------------------------- UI

@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    yield app


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


def test_search_view_set_results_is_sorted_by_score_and_favorites_filter(search_view):
    view = search_view
    view.history_mgr.add_favorite("/p/f2.txt")
    view.set_results(SearchResponse(results=_results(5), total_count=5))
    tree = view.results_tree
    assert tree.topLevelItemCount() == 5
    assert tree.topLevelItem(0).result.name == "f4.txt"
    starred = [tree.topLevelItem(i).text(0) for i in range(5) if "f2" in tree.topLevelItem(i).text(0)]
    assert starred and starred[0].startswith("⭐")

    view.fav_btn.setChecked(True)
    view.toggle_fav_filter()
    # a new result set must respect the active favourites filter
    view.set_results(SearchResponse(results=_results(5), total_count=5))
    hidden = [tree.topLevelItem(i).isHidden() for i in range(5)]
    assert hidden.count(False) == 1


def test_search_view_handles_many_results_quickly(search_view):
    view = search_view
    start = time.time()
    view.set_results(SearchResponse(results=_results(5000), total_count=5000))
    assert view.results_tree.topLevelItemCount() == 5000
    assert time.time() - start < 10


def test_search_view_name_sort_ignores_star_and_case(search_view):
    view = search_view
    view.history_mgr.add_favorite("/p/zeta.txt")
    res = [
        SearchResult("/p/zeta.txt", "zeta.txt", ".txt", 1, 1, False, 1.0),
        SearchResult("/p/Alpha.txt", "Alpha.txt", ".txt", 1, 1, False, 1.0),
        SearchResult("/p/beta.txt", "beta.txt", ".txt", 1, 1, False, 1.0),
    ]
    view.set_results(SearchResponse(results=res, total_count=3))
    from PyQt6.QtCore import Qt

    view.results_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    order = [view.results_tree.topLevelItem(i).result.name for i in range(3)]
    assert order == ["Alpha.txt", "beta.txt", "zeta.txt"]


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


def test_delete_removes_from_index_and_export_list(search_view, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    from fast_file_search.ui.search_view import SearchResultItem

    victim = tmp_path / "victim.txt"
    victim.write_text("x")
    view = search_view
    view.index.add_file(_file(str(victim).replace("\\", "/")))
    path = str(victim).replace("\\", "/")
    res = SearchResult(path, "victim.txt", ".txt", 1, 1, False, 1.0)
    view.set_results(SearchResponse(results=[res], total_count=1))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    item = view.results_tree.topLevelItem(0)
    assert isinstance(item, SearchResultItem)
    view.delete_file(item)
    assert not victim.exists()
    assert view.results_tree.topLevelItemCount() == 0
    assert view.raw_results == []
    assert not view.index.file_exists(path)


# ---- browser view

def test_breadcrumb_navigation_does_not_trigger_indexing(qapp, tmp_path):
    from fast_file_search.ui.browser_view import BrowserView

    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    view = BrowserView()
    navigated, indexed = [], []
    view.path_changed.connect(navigated.append)
    view.select_folder.connect(indexed.append)
    view.set_root_path(str(sub))

    from PyQt6.QtWidgets import QPushButton

    buttons = [b for b in view.breadcrumb_bar.buttons_container.findChildren(QPushButton)]
    assert buttons, "breadcrumb buttons expected"
    buttons[-2].click()                 # click the parent crumb
    assert navigated and not indexed    # navigation only; nothing added to the index

    view.breadcrumb_bar.folder_chosen.emit(str(sub))   # explicit Select Folder does index
    assert indexed == [str(sub)]


def test_breadcrumb_targets_and_separators(qapp, tmp_path):
    from PyQt6.QtWidgets import QLabel, QPushButton
    from fast_file_search.ui.browser_view import BreadcrumbBar

    bar = BreadcrumbBar()
    sub = tmp_path / "one" / "two"
    sub.mkdir(parents=True)
    bar.set_path(str(sub))
    layout = bar.buttons_layout
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert isinstance(widgets[0], QPushButton) and isinstance(widgets[-1], QPushButton)  # no dangling separator
    assert sum(isinstance(w, QLabel) for w in widgets) == sum(isinstance(w, QPushButton) for w in widgets) - 1
    emitted = []
    bar.path_selected.connect(emitted.append)
    widgets[-1].click()
    assert os.path.normcase(emitted[-1]) == os.path.normcase(str(sub))
    widgets[-3].click()
    assert os.path.normcase(emitted[-1]) == os.path.normcase(str(sub.parent))


def test_breadcrumb_recent_history_is_bounded(qapp, tmp_path):
    from fast_file_search.ui.browser_view import BreadcrumbBar

    bar = BreadcrumbBar()
    for i in range(40):
        d = tmp_path / f"d{i}"
        d.mkdir()
        bar.set_path(str(d))
    assert bar.recent_combo.count() == BreadcrumbBar.MAX_RECENT + 1


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are Windows specific")
def test_breadcrumb_handles_unc_and_drive_root(qapp):
    from PyQt6.QtWidgets import QPushButton
    from fast_file_search.ui.browser_view import BreadcrumbBar

    bar = BreadcrumbBar()
    bar.set_path("\\\\server\\share\\folder")
    btns = bar.buttons_container.findChildren(QPushButton)
    emitted = []
    bar.path_selected.connect(emitted.append)
    texts = [b.text() for b in btns]
    assert "\\\\server\\share" in texts and "folder" in texts
    next(b for b in btns if b.text() == "folder").click()
    assert emitted[-1] == "\\\\server\\share\\folder"
    bar.set_path("C:\\")
    btns = bar.buttons_container.findChildren(QPushButton)
    assert [b.text() for b in btns] == ["C:"]


def test_browser_accepts_folder_drops(qapp, tmp_path):
    from PyQt6.QtCore import QMimeData, QPointF, QUrl, Qt
    from PyQt6.QtGui import QDropEvent
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
    # files (not folders) are ignored
    f = tmp_path / "f.txt"
    f.write_text("x")
    mime2 = QMimeData()
    mime2.setUrls([QUrl.fromLocalFile(str(f))])
    ev2 = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime2,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(ev2)
    assert len(got) == 1


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
    monkeypatch.setattr(config_manager.config, "exclude_patterns", [tmp_path.name])
    assert view.get_folder_indexing_status(idx) == "excluded"


# ---- widgets

def test_reset_filters_emits_once(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    panel.update_extensions([".pdf", ".txt"])
    panel.ext_combo.setCurrentIndex(1)
    panel.size_slider.setValue(3)
    panel.regex_input.setText("abc")
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


def test_invalid_regex_filter_is_ignored(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    panel.regex_input.setText("([")
    assert panel.get_filter_values()["regex"] is None
    assert "D32F2F" in panel.regex_input.styleSheet()
    panel.regex_input.setText(r"src\\")
    assert panel.get_filter_values()["regex"] == r"src\\"
    assert panel.regex_input.styleSheet() == ""


def test_update_extensions_preserves_selection(qapp):
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    panel.update_extensions([".pdf", ".txt"])
    panel.ext_combo.setCurrentText(".txt")
    panel.update_extensions([".txt", ".md"])
    assert panel.get_filter_values()["extension"] == ".txt"
    panel.update_extensions([".md"])      # selection vanished from the top list: still kept
    assert panel.get_filter_values()["extension"] == ".txt"


def test_date_presets_use_calendar_days(qapp):
    from PyQt6.QtCore import QDate
    from fast_file_search.ui.widgets import FilterPanel

    panel = FilterPanel()
    midnight = QDate.currentDate().startOfDay().toSecsSinceEpoch()
    panel.date_combo.setCurrentText("Today")
    lo, hi = panel.get_filter_values()["date_range"]
    assert lo == midnight and hi >= lo
    panel.date_combo.setCurrentText("Yesterday")
    lo, hi = panel.get_filter_values()["date_range"]
    assert hi == midnight - 1 and lo == QDate.currentDate().addDays(-1).startOfDay().toSecsSinceEpoch()
    panel.date_combo.setCurrentText("Custom Range")
    panel.start_date_edit.setDate(QDate(2024, 5, 10))
    panel.end_date_edit.setDate(QDate(2024, 5, 1))   # reversed on purpose
    lo, hi = panel.get_filter_values()["date_range"]
    assert lo < hi


def test_preview_panel_plain_text_single_highlighter_and_metadata(qapp, tmp_path):
    from fast_file_search.ui.widgets import PreviewPanel

    panel = PreviewPanel()
    html_file = tmp_path / "page.html"
    html_file.write_text("<html><body><b>needle</b> hay</body></html>", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("needle again", encoding="utf-8")
    panel.set_search_terms(["needle"])

    def wait_for(pred):
        deadline = time.time() + 10
        while not pred() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert pred()

    panel.load_file(str(html_file))
    wait_for(lambda: "needle" in panel.text_preview.toPlainText())
    # source is shown verbatim, not rendered as HTML
    assert "<b>needle</b>" in panel.text_preview.toPlainText()
    wait_for(lambda: len(panel.meta_labels["MD5 Hash"].text()) == 32)
    assert len(panel.meta_labels["SHA256 Hash"].text()) == 64
    # timestamps come from the real file, not the 1970 epoch
    assert not panel.meta_labels["Modified"].text().startswith("1970")
    assert panel.meta_labels["Modified"].text()[:2] == "20"

    first = panel.highlighter
    assert first is not None
    panel.load_file(str(other))
    wait_for(lambda: panel.text_preview.toPlainText() == "needle again")
    assert panel.highlighter is first          # reused, not stacked on the document
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
    deadline = time.time() + 10
    while time.time() < deadline and panel.text_preview.toPlainText() != "file number 5":
        qapp.processEvents()
        time.sleep(0.01)
    assert panel.text_preview.toPlainText() == "file number 5"
    panel.shutdown()
    assert not panel._retired_threads or all(not t.isRunning() for t in panel._retired_threads)


def test_preview_binary_and_missing_files(qapp, tmp_path):
    from fast_file_search.ui.widgets import PreviewLoaderThread

    assert PreviewLoaderThread._decode_preview(b"MZ\x90\x00\x03\x00") is None
    assert PreviewLoaderThread._decode_preview("héllo".encode("utf-16")) == "\ufeffhéllo" or \
        "héllo" in PreviewLoaderThread._decode_preview("héllo".encode("utf-16"))
    assert PreviewLoaderThread._decode_preview("plain".encode()) == "plain"


def test_find_resource_locates_icon_and_misses_cleanly():
    from fast_file_search.ui.widgets import find_resource

    icon = find_resource("files.ico")
    assert icon is not None and icon.name == "files.ico"
    assert find_resource("definitely-not-bundled.xyz") is None


def test_highlighter_set_terms_rebuilds_rules(qapp):
    from PyQt6.QtGui import QTextDocument
    from fast_file_search.ui.widgets import MatchHighlighter

    doc = QTextDocument()
    h = MatchHighlighter(doc, ["a.b", "x"])      # 1-char terms ignored; metacharacters escaped
    assert len(h.rules) == 1 and h.rules[0][0].search("a.b") and not h.rules[0][0].search("axb")
    h.set_terms([])
    assert h.rules == []


def test_settings_dialog_detects_equivalent_paths(qapp, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog
    from fast_file_search.ui.widgets import SettingsDialog

    dlg = SettingsDialog()
    dlg.paths_list.clear()
    forward = str(tmp_path).replace("\\", "/")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: forward))
    dlg.add_indexed_path()
    dlg.add_indexed_path()
    assert dlg.paths_list.count() == 1
    assert dlg.paths_list.item(0).text() == os.path.normpath(forward)
    if sys.platform == "win32":
        monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: forward.upper()))
        dlg.add_indexed_path()
        assert dlg.paths_list.count() == 1
