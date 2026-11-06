"""Filter chips: one request per change with the right FilterState (design F.2)."""
import time

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest

from fast_file_search.engine.protocol import FilterState
from fast_file_search.ui.filter_chips import ChipState, FilterChipBar, parse_size

from fake_engine import *  # noqa: F401,F403
from fake_engine import search

KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3


def _requests_after(client, n):
    return client.searches[n:]


def _one_request(window, client, action):
    before = len(client.searches)
    action()
    new = _requests_after(client, before)
    assert len(new) == 1, f"expected one request, got {len(new)}"
    return new[0].filters


@pytest.fixture
def win(window, qtbot):
    search(qtbot, window, "gift")
    return window


def test_type_presets_and_extensions(win, client):
    chips = win.chips
    f = _one_request(win, client, lambda: chips.type_actions["image"].trigger())
    assert f.kinds == frozenset({"image"}) and not f.exts
    assert chips.type_chip.button.text() == "Type: Images" and chips.type_chip.clear_btn.isVisible()
    f = _one_request(win, client, lambda: chips.type_actions["folder"].trigger())
    assert f.kinds == frozenset({"folder"})

    def exts():
        chips.ext_edit.setText(".PDF, docx")
        chips.ext_edit.returnPressed.emit()
    f = _one_request(win, client, exts)
    assert f.exts == frozenset({"pdf", "docx"}) and not f.kinds
    assert chips.type_chip.label() == "Type: pdf, docx"
    f = _one_request(win, client, chips.type_chip.clear_btn.click)
    assert f == FilterState(**{**f.__dict__, "kinds": frozenset(), "exts": frozenset()})
    assert not chips.type_chip.clear_btn.isVisible() and chips.type_chip.label() == "Type: All"


def test_size_presets_and_custom(win, client):
    chips = win.chips
    f = _one_request(win, client, lambda: chips.size_actions["tiny"].trigger())
    assert (f.size_min, f.size_max) == (None, 10 * KB)
    assert chips.size_chip.label() == "Size: Under 10 KB"
    assert "files only" in chips.size_chip.button.toolTip()
    f = _one_request(win, client, lambda: chips.size_actions["huge"].trigger())
    assert (f.size_min, f.size_max) == (GB, None)

    chips.size_min_edit.setText("10 kb")
    chips.size_max_edit.setText("2.5 MB")
    f = _one_request(win, client, chips._apply_custom_size)
    assert (f.size_min, f.size_max) == (10 * KB, int(2.5 * MB))

    before = len(client.searches)
    chips.size_min_edit.setText("ten")
    assert chips._apply_custom_size() is False
    assert not chips.size_error.isHidden() and "not a size" in chips.size_error.text()
    chips.size_min_edit.setText("5 MB")
    chips.size_max_edit.setText("1 MB")
    assert chips._apply_custom_size() is False and "smaller" in chips.size_error.text()
    assert len(client.searches) == before                       # invalid input sends nothing


def test_modified_presets_resolve_to_timestamps(win, client):
    chips = win.chips
    midnight = QDate.currentDate().startOfDay().toSecsSinceEpoch()
    f = _one_request(win, client, lambda: chips.date_actions["today"].trigger())
    assert f.mtime_min == midnight and f.mtime_max is None
    f = _one_request(win, client, lambda: chips.date_actions["yesterday"].trigger())
    assert f.mtime_max == midnight and f.mtime_min == QDate.currentDate().addDays(-1).startOfDay().toSecsSinceEpoch()
    f = _one_request(win, client, lambda: chips.date_actions["week"].trigger())
    assert abs(f.mtime_min - (time.time() - 7 * 86400)) < 120
    # Rounded to the minute: two requests with the same preset are identical.
    assert chips.state() == chips.state()

    chips.date_from_edit.setDate(QDate(2024, 5, 10))
    chips.date_to_edit.setDate(QDate(2024, 5, 1))            # reversed on purpose
    f = _one_request(win, client, lambda: chips._update(date_key="custom", date_from="2024-05-10",
                                                        date_to="2024-05-01"))
    assert f.mtime_min == QDate(2024, 5, 1).startOfDay().toSecsSinceEpoch()
    assert f.mtime_max == QDate(2024, 5, 11).startOfDay().toSecsSinceEpoch()


def test_path_chip_root_folder_and_contains(win, client, dataset):
    chips = win.chips
    chips._fill_path_menu()
    downloads = next(a for a in chips.path_menu.actions() if a.text() == "Downloads")
    f = _one_request(win, client, downloads.trigger)
    assert f.root_ids == frozenset({1}) and f.folder is None
    assert chips.path_chip.label() == "Path: Downloads"

    sub = dataset.roots[1] + "\\icons" if "\\" in dataset.roots[1] else dataset.roots[1] + "/icons"
    f = _one_request(win, client, lambda: chips.set_folder(sub))
    assert f.folder == sub and not f.root_ids
    assert chips.path_chip.label() == "Path: icons"
    assert chips.scope_root_id() == 1

    chips._fill_path_menu()
    chips.contains_edit.setText("assets")
    f = _one_request(win, client, chips._apply_path_contains)
    assert f.path_contains == "assets"
    f = _one_request(win, client, chips.path_chip.clear_btn.click)
    assert f.folder is None and not f.root_ids and f.path_contains is None


def test_choose_folder_outside_roots_offers_to_add(win, client, monkeypatch, tmp_path, no_modal_dialogs):
    from PySide6.QtWidgets import QFileDialog

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(outside)))
    before = len(client.searches)
    win.chips.choose_folder()
    assert len(client.searches) == before
    assert any(e[0] == "exec" and "Not in an indexed folder" in e[1] for e in no_modal_dialogs)


def test_exclude_editor_and_recent_lists(win, client):
    chips = win.chips
    chips.exclude_edit.setText("node_modules, *.tmp")
    f = _one_request(win, client, chips._apply_excludes)
    assert f.excludes == ("node_modules", "*.tmp")
    assert chips.exclude_chip.label() == "Exclude: node_modules, *.tmp"
    assert chips.recent_excludes()[0] == ["node_modules", "*.tmp"]
    before = len(client.searches)
    chips._apply_excludes()            # unchanged text on menu close: no request
    assert len(client.searches) == before
    chips._fill_exclude_menu()
    recent = [a for a in chips.exclude_menu.actions() if getattr(a, "_recent", False)]
    assert recent and recent[0].text() == "node_modules, *.tmp"


def test_editors_apply_on_menu_close_not_per_keystroke(win, client):
    chips = win.chips
    before = len(client.searches)
    for ch in "pdf":
        QTest.keyClicks(chips.ext_edit, ch)
    assert len(client.searches) == before
    chips.type_menu.aboutToHide.emit()
    assert len(client.searches) == before + 1 and client.last.filters.exts == frozenset({"pdf"})


def test_favorites_chip_only_while_on(win, client):
    chips = win.chips
    assert chips.fav_chip.isHidden()
    f = _one_request(win, client, lambda: chips.set_favorites(True))
    assert f.favorites_only and not chips.fav_chip.isHidden()
    f = _one_request(win, client, chips.fav_chip.clear_btn.click)
    assert not f.favorites_only and chips.fav_chip.isHidden()


def test_clear_filters_one_request_and_resets_sidebar(win, client):
    chips = win.chips
    chips._update(type_key="image", size_key="tiny", root_id=1, excludes=("x",))
    assert chips.clear_btn.isVisible() and win.sidebar.selected_root() == 1
    f = _one_request(win, client, chips.clear_btn.click)
    assert f == FilterState()
    assert not chips.clear_btn.isVisible()
    assert win.sidebar.selected_root() is None
    f = _one_request(win, client, lambda: (chips._update(type_key="code"), None)[1] or None) if False else None
    chips._update(type_key="code")
    f = _one_request(win, client, win.act_clear_filters.trigger)          # Ctrl+Shift+X
    assert f == FilterState()


def test_labels_elide_and_accessible_names(qapp):
    bar = FilterChipBar()
    labels = [c.button.text() for c in bar.chips[:5]]
    assert labels == ["Type: All", "Size: Any", "Modified: Any time", "Path: All folders", "Exclude: None"]
    bar._update(excludes=("node_modules", "*.tmp", "build", "dist", "coverage"))
    text = bar.exclude_chip.button.text()
    assert len(text) <= 28 and text.endswith("…")
    assert "coverage" in bar.exclude_chip.button.toolTip()
    assert bar.type_chip.button.accessibleName() == "Type filter, All"
    assert bar.size_chip.clear_btn.accessibleName() == "Clear Size filter"


def test_set_state_round_trip(qapp):
    bar = FilterChipBar()
    s = FilterState(kinds=frozenset({"image"}), size_min=None, size_max=10 * KB, excludes=("a",),
                    path_contains="src", favorites_only=True)
    bar.set_state(s)
    assert bar.state() == s
    assert bar.emit_count == 0                      # set_state is silent by default
    bar.set_state(FilterState(exts=frozenset({"pdf"})), emit=True)
    assert bar.emit_count == 1 and bar.state().exts == frozenset({"pdf"})


@pytest.mark.parametrize("text,value", [("", None), ("512", 512), ("10 kb", 10 * KB), ("2.5 MB", int(2.5 * MB)),
                                        ("1,5 g", int(1.5 * GB)), ("3 bytes", 3)])
def test_parse_size(text, value):
    assert parse_size(text) == value


def test_parse_size_rejects_garbage():
    with pytest.raises(ValueError):
        parse_size("10 parsecs")


def test_chip_click_opens_menu_without_toggling(qapp, qtbot):
    bar = FilterChipBar()
    bar.show()
    btn = bar.type_chip.button
    assert not btn.isChecked()
    btn.nextCheckState()
    assert not btn.isChecked()                      # only the bar decides "checked"
    assert ChipState().is_default()
