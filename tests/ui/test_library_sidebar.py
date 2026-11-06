"""Library sidebar: rows, live per-root counts, Path chip sync, smart filters (design F.3)."""
import os

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent, QKeySequence
from PySide6.QtTest import QTest

from fast_file_search.ui.library_sidebar import CountRole, LibrarySidebar, RootIdRole

from fake_engine import *  # noqa: F401,F403
from fake_engine import FakeEngineClient, search, wait_result


def _row(win, name):
    m = win.sidebar.model
    for i in range(m.rowCount()):
        if m.index(i).data() == name:
            return m.index(i)
    raise AssertionError(name)


def test_rows_and_entry_counts_without_a_query(window, dataset):
    m = window.sidebar.model
    names = [m.index(i).data() for i in range(m.rowCount())]
    assert names == ["Downloads", "Desktop", "Documents", "Projects", "All indexed folders"]
    st = window._status
    assert _row(window, "Desktop").data(CountRole) == next(r.entries for r in st.roots if r.name == "Desktop")
    assert _row(window, "All indexed folders").data(CountRole) == st.total_entries
    assert window.sidebar.selected_root() is None                    # "All indexed folders" by default
    assert "items" in _row(window, "Documents").data(Qt.ItemDataRole.AccessibleTextRole)


def test_counts_follow_the_query_and_ignore_root_scope(window, client, qtbot):
    search(qtbot, window, "gifttopicon")
    counts = dict(window._last_result.root_counts)
    assert _row(window, "Downloads").data(CountRole) == counts[1]
    assert _row(window, "All indexed folders").data(CountRole) == sum(counts.values())
    acc = _row(window, "Downloads").data(Qt.ItemDataRole.AccessibleTextRole)
    assert acc == f"Downloads, {counts[1]} results"
    # Scoping to Downloads keeps the other roots' counts (computed before the root scope).
    window.sidebar.list.setCurrentIndex(_row(window, "Downloads"))
    wait_result(qtbot, window)
    assert client.last.filters.root_ids == frozenset({1})
    assert window._last_result.total == counts[1]
    assert _row(window, "Desktop").data(CountRole) == counts[2]


def test_sidebar_and_path_chip_are_one_state(window, client, qtbot):
    search(qtbot, window, "gift")
    before = len(client.searches)
    window.sidebar.list.setCurrentIndex(_row(window, "Documents"))
    assert len(client.searches) == before + 1
    assert client.last.filters.root_ids == frozenset({3})
    assert window.chips.path_chip.label() == "Path: Documents"
    # Chip -> sidebar: selection follows silently (still one request).
    before = len(client.searches)
    window.chips.set_root(1)
    assert len(client.searches) == before + 1
    assert window.sidebar.selected_root() == 1
    window.chips.path_chip.clear_btn.click()
    assert window.sidebar.selected_root() is None
    # A folder scope selects its root in the sidebar.
    window.chips.set_folder(os.path.join(client.dataset.roots[4], "web"))
    assert window.sidebar.selected_root() == 4


def test_smart_filters_mirror_type_and_modified_chips(window, client, qtbot):
    search(qtbot, window, "gift")
    sm = window.sidebar.smart_model
    sl = window.sidebar.smart_list

    def click(key):
        row = [sm.index(i).data(Qt.ItemDataRole.UserRole + 6) for i in range(sm.rowCount())].index(key)
        before = len(client.searches)
        sl.clicked.emit(sm.index(row))
        assert len(client.searches) == before + 1
        return client.last.filters

    f = click("image")
    assert f.kinds == frozenset({"image"}) and window.chips.type_chip.label() == "Type: Images"
    f = click("code")                                  # mutually exclusive with Images
    assert f.kinds == frozenset({"code"})
    assert sm.checked["code"] and not sm.checked["image"]
    f = click("code")                                  # toggling off
    assert not f.kinds
    f = click("recent")
    assert f.mtime_min is not None and window.chips.date_chip.label() == "Modified: Past 7 days"
    f = click("favorites")
    assert f.favorites_only and not window.chips.fav_chip.isHidden()
    # Chip -> smart filter mirror.
    window.chips.type_actions["document"].trigger()
    assert sm.checked["document"]
    window.chips.date_actions["any"].trigger()
    assert not sm.checked["recent"]


def test_root_states_icons_tooltips_and_notices(make_window, dataset, qtbot):
    client = FakeEngineClient(dataset, ready=False)
    win = make_window(client)
    client.make_ready(dataset.status(root_state={2: "denied", 3: "missing", 4: "scanning"}, state="scanning",
                                     scanned=120))
    qtbot.waitUntil(lambda: win._status is not None and win._status.state == "scanning", timeout=1000)
    assert "denied" in _row(win, "Desktop").data(Qt.ItemDataRole.ToolTipRole).lower() \
        or "allow" in _row(win, "Desktop").data(Qt.ItemDataRole.ToolTipRole)
    assert "not found" in _row(win, "Documents").data(Qt.ItemDataRole.ToolTipRole).lower()
    assert win.sidebar._spin.isActive()                 # spinner animates only while scanning
    assert win.notice_bar.isVisible()
    assert win.notice_bar.has("root:2") and win.notice_bar.has("root:3")
    client.push_status(dataset.status())
    assert not win.sidebar._spin.isActive()
    assert not win.notice_bar.has("root:2")


def test_context_menu_actions(window, client, monkeypatch):
    sb = window.sidebar
    sb.rescan_requested.emit(2)
    assert client.index_calls[-1] == ("rescan", (2,))
    monkeypatch.setattr(window, "_confirm", lambda *a, **k: True)
    path = client.dataset.roots[3]
    sb.remove_requested.emit(3, path)
    assert path not in window.cm.config.index_paths
    assert client.configs and path not in client.configs[-1].roots


def test_context_menu_contents(window, qtbot):
    sb = window.sidebar
    rect = sb.list.visualRect(_row(window, "Downloads"))
    sb._context_menu(rect.center())
    texts = [a.text() for a in sb._last_menu.actions() if not a.isSeparator()]
    assert texts[:2] == ["Rescan", __import__("fast_file_search.ui.theme", fromlist=["x"]).reveal_label()]
    assert "Remove from index…" in texts and "Exclude a subfolder…" in texts and texts[-1] == "Add folder…"
    sb._last_menu.close()


def test_exclude_a_subfolder_adds_a_sequence(window, client, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    root = client.dataset.roots[1]
    sub = os.path.join(root, "icons", "old")
    os.makedirs(sub, exist_ok=True)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: sub))
    window.exclude_subfolder(1, root)
    assert "Downloads/icons/old" in window.cm.config.exclude_patterns
    assert "Downloads/icons/old" in client.configs[-1].exclude_patterns


def test_ctrl_digits_select_roots(window, client, qtbot):
    search(qtbot, window, "gift")
    for act in window.root_actions[:3]:
        assert act.shortcuts()
    assert QKeySequence("Ctrl+2") in window.root_actions[2].shortcuts()
    window.root_actions[2].trigger()
    assert client.last.filters.root_ids == frozenset({2}) and window.sidebar.selected_root() == 2
    window.root_actions[0].trigger()
    assert not client.last.filters.root_ids and window.sidebar.selected_root() is None


def test_folder_drop_adds_a_root(window, client, tmp_path):
    new = tmp_path / "Music"
    new.mkdir()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(new))])
    ev = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier)
    window.sidebar.dropEvent(ev)
    assert os.path.normpath(str(new)) in window.cm.config.index_paths
    assert os.path.normpath(str(new)) in client.configs[-1].roots
    f = tmp_path / "f.txt"
    f.write_text("x")
    n = len(client.configs)
    mime2 = QMimeData()
    mime2.setUrls([QUrl.fromLocalFile(str(f))])
    window.sidebar.dropEvent(QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime2,
                                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert len(client.configs) == n                     # files are ignored


def test_disambiguated_names_come_from_the_engine(qapp, dataset):
    sb = LibrarySidebar()
    st = dataset.status(names={1: "Downloads (alex)", 2: "Downloads (work)"})
    sb.set_roots(st.roots)
    assert sb.model.index(0).data() == "Downloads (alex)"
    assert sb.model.index(0).data(RootIdRole) == 1
    assert sb.model.index(sb.model.rowCount() - 1).data(RootIdRole) is None


def test_settings_button(window, monkeypatch):
    opened = []
    monkeypatch.setattr(window, "open_settings", lambda: opened.append(1))
    window.sidebar.settings_requested.disconnect()
    window.sidebar.settings_requested.connect(window.open_settings)
    window.sidebar.settings_btn.click()
    assert opened == [1]
    assert window.sidebar.settings_btn.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
