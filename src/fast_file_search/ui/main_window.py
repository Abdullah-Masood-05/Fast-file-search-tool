"""Main window (design F.1): search bar, filter chips, notice bar, then library
sidebar | results | preview, and the status bar.

The window holds no index: every search, page, favorite and index command goes
through the ``EngineClient`` (a separate engine process). Nothing here imports
sqlite3 or does file-system work beyond what the user explicitly asks for.
"""
from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QEvent, QModelIndex, QTimer, QUrl, Qt
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QSplitter, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from ..engine.protocol import EngineConfig, FilterState, IndexStatus, Notice, SearchResult, SortSpec
from ..utils.platform_utils import is_path_under, paths_equal
from . import file_actions, theme
from .filter_chips import FilterChipBar
from .library_sidebar import LibrarySidebar
from .onboarding import OnboardingDialog, open_privacy_settings
from .preview import PreviewPane
from .results import PAGE_SIZE, ResultsPane, summary_text
from .search_bar import SearchBar
from .theme import APP_NAME, SPACE_M, SPACE_S, SPACE_XS, display_text, format_count
from .widgets import (
    AboutDialog, NoticeBar, SettingsDialog, StatusDot, app_icon, app_version, export_results, kinds,
    log_dir,
)

IDLE_COMMIT_MS = 2000
MAX_ROOT_SHORTCUTS = 9


def _plural(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{format_count(n)} {word if n == 1 else (plural or word + 's')}"


class MainWindow(QMainWindow):
    """See the module docstring. ``client`` is an ``EngineClient`` (or the test fake)."""

    def __init__(self, client, config_manager, *, first_run: bool = False):
        super().__init__()
        self.client = client
        self.config_manager = config_manager
        self.first_run = first_run
        self._shutdown_done = False
        self.setWindowTitle(APP_NAME)
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        theme.apply_appearance(theme.current_appearance())

        # search bookkeeping
        self._gen = -1
        self._req_key: Optional[tuple] = None        # (text, chip state, sort) of the latest request
        self._req_raw = ""                            # field text of the latest request
        self._req_empty = True                        # latest request had no query and no filters
        self._displayed_key: Optional[tuple] = None
        self._committed_key: Optional[tuple] = None
        self._pending_send: Optional[bool] = None     # commit flag of a request queued until Ready
        self._last_result: Optional[SearchResult] = None
        self._status: Optional[IndexStatus] = getattr(client, "last_status", None)
        self._engine_state = "ready" if getattr(client, "is_ready", False) else "starting"
        self._pending_info: Dict[int, tuple] = {}
        self.last_apply_ms = 0.0
        self.request_count = 0

        self._idle_commit = QTimer(self)
        self._idle_commit.setSingleShot(True)
        self._idle_commit.setInterval(IDLE_COMMIT_MS)
        self._idle_commit.timeout.connect(self._commit_idle)

        self._build_ui()
        self._build_actions()
        self._build_menus()
        self.setup_system_tray()
        self._restore_ui_state()
        self._connect_client()

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)

        if self._status is not None:
            self._on_index_status(self._status)
        self._refresh_state()
        self._update_status_bar()
        self._update_item_actions()
        self.bar.field.setFocus()
        if first_run and not self._configured_roots():
            QTimer.singleShot(0, self.show_onboarding)

    # ------------------------------------------------------------------ layout
    def _build_ui(self) -> None:
        self.setMinimumSize(900, 560)
        central = QWidget()
        central.setObjectName("Central")
        self.central_widget = central
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(SPACE_M, SPACE_M, SPACE_M, SPACE_S)
        layout.setSpacing(SPACE_S)

        self.bar = SearchBar()
        layout.addWidget(self.bar)
        self.chips = FilterChipBar()
        layout.addWidget(self.chips)
        self.notice_bar = NoticeBar()
        layout.addWidget(self.notice_bar)

        self.sidebar = LibrarySidebar()
        self.results = ResultsPane()
        self.preview = PreviewPane(self.client)
        self.preview.setMinimumWidth(240)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("MainSplitter")
        self.main_splitter.setHandleWidth(SPACE_M)
        self.main_splitter.setChildrenCollapsible(False)
        for w in (self.sidebar, self.results, self.preview):
            self.main_splitter.addWidget(w)
        for i, stretch in enumerate((0, 1, 0)):
            self.main_splitter.setStretchFactor(i, stretch)
        self.main_splitter.setSizes([220, 700, 360])
        layout.addWidget(self.main_splitter, 1)

        # status bar
        status = self.statusBar()
        status.setSizeGripEnabled(False)
        self.status_bar = status
        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(SPACE_XS, 0, 0, 0)
        line.setSpacing(SPACE_XS)
        self.status_dot = StatusDot()
        self.status_lbl = QLabel("Loading index…")
        self.status_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.status_lbl.setAccessibleName("Index status")
        self.count_lbl = QLabel("")
        self.count_lbl.setAccessibleName("Items indexed")
        self.count_lbl.setFont(theme.tabular_font())
        theme.make_secondary(self.count_lbl)
        line.addWidget(self.status_dot)
        line.addWidget(self.status_lbl)
        line.addSpacing(SPACE_M)
        line.addWidget(self.count_lbl)
        line.addStretch(1)
        status.addWidget(holder, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(120)
        self.progress_bar.setMaximumHeight(self.fontMetrics().height() - 2)
        self.progress_bar.setAccessibleName("Indexing progress")
        status.addPermanentWidget(self.progress_bar)
        self.live_lbl = QLabel("")
        self.live_lbl.setAccessibleName("Live monitoring")
        self.live_lbl.setContentsMargins(0, 0, SPACE_S, 0)
        theme.make_secondary(self.live_lbl)
        status.addPermanentWidget(self.live_lbl)

        # wiring
        b = self.bar
        b.search_requested.connect(self._on_search_requested)
        b.open_requested.connect(self._open_current_or_first)
        b.nav_key.connect(self._on_nav_key)
        b.escape_to_hide.connect(self._hide_from_escape)
        b.cleared.connect(self._on_search_cleared)
        b.copy_requested.connect(self.copy_selected_path)
        b.keystroke.connect(self._idle_commit.stop)
        self.chips.filters_changed.connect(self._on_filters_changed)
        self.chips.add_root_requested.connect(lambda p: self.add_roots([p]))
        s = self.sidebar
        s.root_selected.connect(self._on_sidebar_root)
        s.smart_toggled.connect(self._on_smart_toggled)
        s.settings_requested.connect(self.open_settings)
        s.add_folder_requested.connect(self.add_folder)
        s.rescan_requested.connect(lambda rid: self.client.index("rescan", (rid,)))
        s.reveal_requested.connect(lambda p: self._report([file_actions.reveal(p)]))
        s.remove_requested.connect(self.remove_root)
        s.exclude_requested.connect(self.exclude_subfolder)
        s.open_privacy_requested.connect(lambda: open_privacy_settings(False))
        s.folders_dropped.connect(self.add_roots)
        r = self.results
        r.sort_changed.connect(lambda _spec: self._send_search(commit=False))
        r.model.fetch_requested.connect(self._fetch_page)
        r.model.modelReset.connect(self._update_item_actions)
        r.view.open_requested.connect(self.open_selected)
        r.view.reveal_requested.connect(self.reveal_selected_path)
        r.view.focus_search_requested.connect(lambda: self.bar.focus(select_all=False))
        r.view.doubleClicked.connect(lambda _i: self.open_selected())
        r.view.customContextMenuRequested.connect(self._results_context_menu)
        r.view.selectionModel().currentRowChanged.connect(self._on_current_row)
        r.view.selectionModel().selectionChanged.connect(lambda *_: self._update_item_actions())
        r.empty_state.link.linkActivated.connect(self._apply_did_you_mean)
        p = self.preview
        p.open_requested.connect(lambda path: self.open_paths([path]))
        p.reveal_requested.connect(lambda path: self._report([file_actions.reveal(path)]))
        p.copy_path_requested.connect(lambda path: self.copy_paths([path]))
        p.scope_requested.connect(lambda path: self.chips.set_folder(path, ask_to_add=True))

        # Tab order: field -> Search -> chips -> sidebar -> results -> preview.
        chain: List[QWidget] = [b.field, b.search_btn]
        for chip in self.chips.chips:
            chain += [chip.button, chip.clear_btn]
        chain += [self.chips.clear_btn, s.list, s.smart_list, s.settings_btn, r.sort_button, r.view]
        for a, c in zip(chain, chain[1:]):
            QWidget.setTabOrder(a, c)
        QWidget.setTabOrder(r.view, self.preview)

    # ------------------------------------------------------------------ actions & menus
    def _action(self, text: str, slot=None, shortcuts=(), checkable: bool = False,
                role: Optional[QAction.MenuRole] = None, add_to_window: bool = True) -> QAction:
        act = QAction(text, self)
        keys: List[QKeySequence] = []
        for sc in shortcuts:
            keys.extend(QKeySequence.keyBindings(sc) if isinstance(sc, QKeySequence.StandardKey)
                        else [QKeySequence(sc)])
        unique: List[QKeySequence] = []
        for k in keys:
            if not k.isEmpty() and k not in unique:
                unique.append(k)
        if unique:
            act.setShortcuts(unique)
        act.setCheckable(checkable)
        act.setMenuRole(role if role is not None else QAction.MenuRole.NoRole)
        if slot is not None:
            act.triggered.connect(lambda checked=False, s=slot: s(checked) if checkable else s())
        if add_to_window:
            self.addAction(act)  # shortcuts work even when the menu bar is hidden (Linux/KDE)
        return act

    def _build_actions(self) -> None:
        SK = QKeySequence.StandardKey
        ctrl = Qt.KeyboardModifier.ControlModifier
        shift = Qt.KeyboardModifier.ShiftModifier
        self.act_open = self._action("Open", self.open_selected, [SK.Open])
        self.act_open_with = self._action("Open with…", self._open_with_selected)
        self.act_open_with.setVisible(file_actions.open_with_mode() in ("system", "choose-app"))
        self.act_reveal = self._action(theme.reveal_label(), self.reveal_selected_path, [
            QKeySequence(ctrl | Qt.Key.Key_Return), QKeySequence(ctrl | Qt.Key.Key_Enter)])
        trash_keys = [SK.Delete]
        if theme.IS_MAC:
            trash_keys.append(QKeySequence(ctrl | Qt.Key.Key_Backspace))
        # Delete only acts while the results list has focus (never in the field or sidebar).
        self.act_trash = self._action(theme.trash_label(), self.trash_selected, trash_keys, add_to_window=False)
        self.act_trash.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.results.view.addAction(self.act_trash)
        self.act_add_folder = self._action("Add folder…", self.add_folder)
        self.act_export = self._action("Export results…", self.export_results)
        self.act_settings = self._action(f"{theme.settings_label()}…", self.open_settings,
                                         [SK.Preferences, "Ctrl+,"], role=QAction.MenuRole.PreferencesRole)
        self.act_quit = self._action("Exit" if theme.IS_WINDOWS else "Quit", self.close_and_quit,
                                     [SK.Quit], role=QAction.MenuRole.QuitRole)

        self.act_copy_path = self._action("Copy path", self.copy_selected_path, [SK.Copy])
        self.act_copy_name = self._action("Copy name", self.copy_selected_name,
                                          [QKeySequence(ctrl | shift | Qt.Key.Key_C)])
        self.act_find = self._action("Find", self.focus_search, [SK.Find, "Ctrl+K", "Ctrl+L"])
        self.act_clear_filters = self._action("Clear filters", self.clear_filters,
                                              [QKeySequence(ctrl | shift | Qt.Key.Key_X)])

        self.act_sidebar = self._action("Sidebar", self._set_sidebar_visible, checkable=True)
        self.act_preview = self._action("Preview", self._set_preview_visible, checkable=True)
        self.appearance_group = QActionGroup(self)
        self.appearance_actions: Dict[str, QAction] = {}
        for mode, text in (("system", "Use system setting"), ("light", "Light"), ("dark", "Dark")):
            act = QAction(text, self, checkable=True)
            act.setMenuRole(QAction.MenuRole.NoRole)
            act.triggered.connect(lambda _=False, m=mode: self.set_appearance(m))
            self.appearance_group.addAction(act)
            self.appearance_actions[mode] = act
        self.appearance_actions.get(theme.current_appearance(), self.appearance_actions["system"]).setChecked(True)

        self.act_update_index = self._action("Update index", self.update_index, [SK.Refresh, "Ctrl+R"])
        self.act_rebuild_index = self._action("Rebuild index…", self.rebuild_index)
        self.act_diagnostics = self._action("Copy diagnostics", self.copy_diagnostics)
        self.act_logs = self._action("Show log folder", self.show_log_folder)
        self.act_about = self._action(f"About {APP_NAME}", self.open_about, role=QAction.MenuRole.AboutRole)

        self.root_actions: List[QAction] = []
        for n in range(0, MAX_ROOT_SHORTCUTS + 1):
            act = self._action("All indexed folders" if n == 0 else f"Folder {n}",
                               lambda n=n: self.select_root_index(n), [f"Ctrl+{n}"])
            self.root_actions.append(act)

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        for a in (self.act_open, self.act_open_with, self.act_reveal):
            file_menu.addAction(a)
        file_menu.addSeparator()
        file_menu.addAction(self.act_trash)
        file_menu.addSeparator()
        file_menu.addAction(self.act_add_folder)
        file_menu.addAction(self.act_export)
        file_menu.addSeparator()
        file_menu.addAction(self.act_settings)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        edit_menu = bar.addMenu("&Edit")
        edit_menu.addAction(self.act_copy_path)
        edit_menu.addAction(self.act_copy_name)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_find)
        edit_menu.addAction(self.act_clear_filters)

        view_menu = bar.addMenu("&View")
        view_menu.addAction(self.act_sidebar)
        view_menu.addAction(self.act_preview)
        view_menu.addSeparator()
        self.folders_menu = view_menu.addMenu("Search in")
        self.folders_menu.aboutToShow.connect(self._fill_folders_menu)
        view_menu.addSeparator()
        appearance = view_menu.addMenu("Appearance")
        for act in self.appearance_actions.values():
            appearance.addAction(act)

        index_menu = bar.addMenu("&Index")
        index_menu.addAction(self.act_update_index)
        index_menu.addAction(self.act_rebuild_index)
        index_menu.addSeparator()
        index_menu.addAction(self.act_add_folder)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.act_diagnostics)
        help_menu.addAction(self.act_logs)
        help_menu.addSeparator()
        help_menu.addAction(self.act_about)

    def _fill_folders_menu(self) -> None:
        menu = self.folders_menu
        menu.clear()
        roots = self._roots()
        menu.addAction(self.root_actions[0])
        for i, r in enumerate(roots[:MAX_ROOT_SHORTCUTS]):
            act = self.root_actions[i + 1]
            act.setText(display_text(r.name))
            menu.addAction(act)

    # ------------------------------------------------------------------ ui state
    def _restore_ui_state(self) -> None:
        s = theme.ui_settings()
        geometry = s.value("window/geometry_v2")
        if not geometry or not self.restoreGeometry(geometry):
            self.resize(1280, 800)
        state = s.value("window/main_splitter_v2")
        if state:
            self.main_splitter.restoreState(state)
        self._set_sidebar_visible(theme.setting_bool("view/sidebar", True))
        self._set_preview_visible(theme.setting_bool("view/preview", True))

    def _save_ui_state(self) -> None:
        s = theme.ui_settings()
        s.setValue("window/geometry_v2", self.saveGeometry())
        s.setValue("window/main_splitter_v2", self.main_splitter.saveState())
        s.setValue("view/sidebar", self.act_sidebar.isChecked())
        s.setValue("view/preview", self.act_preview.isChecked())
        self.results.save_state()
        s.sync()

    def _set_sidebar_visible(self, visible: bool) -> None:
        self.sidebar.setVisible(bool(visible))
        self.act_sidebar.setChecked(bool(visible))

    def _set_preview_visible(self, visible: bool) -> None:
        self.preview.setVisible(bool(visible))
        self.act_preview.setChecked(bool(visible))

    def set_appearance(self, mode: str) -> None:
        theme.apply_appearance(mode)
        theme.ui_settings().setValue("appearance", mode)
        act = self.appearance_actions.get(mode)
        if act is not None:
            act.setChecked(True)
        self.results.model.palette_changed()

    # ------------------------------------------------------------------ engine signals
    def _connect_client(self) -> None:
        c = self.client
        c.ready.connect(self._on_ready)
        c.search_result.connect(self._on_search_result)
        c.index_status.connect(self._on_index_status)
        c.item_info.connect(self._on_item_info)
        c.notice.connect(self._on_notice)
        c.engine_state.connect(self._on_engine_state)

    def _on_ready(self, ready) -> None:
        self._engine_state = "ready"
        self.notice_bar.remove("engine")
        self._on_index_status(ready.status)
        if self._pending_send is not None:
            commit = self._pending_send
            self._pending_send = None
            self._send_search(commit=commit)
        self._refresh_state()
        self._update_status_bar()

    def _on_engine_state(self, state: str) -> None:
        self._engine_state = state
        if state == "restarting":
            self.notice_bar.post("engine", "The indexer stopped unexpectedly. Restarting…",
                                 dismissible=False, force=True)
        elif state == "failed":
            self.notice_bar.post("engine", "The indexer stopped unexpectedly and could not be restarted.",
                                 [("Restart", self._restart_engine), ("Show log folder", self.show_log_folder)],
                                 level="error", dismissible=False, force=True)
        elif state == "ready":
            self.notice_bar.remove("engine")
        self._update_status_bar()
        self._refresh_state()

    def _restart_engine(self) -> None:
        self.notice_bar.post("engine", "Restarting the indexer…", dismissible=False, force=True)
        try:
            self.client.start()
        except Exception as exc:  # pragma: no cover - defensive
            self.notice_bar.post("engine", f"The indexer could not be started: {exc}",
                                 [("Show log folder", self.show_log_folder)], level="error", force=True)

    def _on_notice(self, n: Notice) -> None:
        data = dict(n.data or ())
        key = f"notice:{n.code}:{data.get('root_id', '')}"
        actions = []
        if n.code == "tcc_denied" and theme.IS_MAC:
            actions.append(("Open Privacy Settings", lambda: open_privacy_settings(False)))
        elif n.code in ("config_recovered", "home_excludes_added"):
            actions.append((f"Open {theme.settings_label()}", self.open_settings))
        elif n.code == "index_rebuilt":
            actions.append(("Show log folder", self.show_log_folder))
        self.notice_bar.post(key, n.text, actions, level=n.level)

    def _on_index_status(self, st: IndexStatus) -> None:
        self._status = st
        roots = tuple(st.roots)
        self.sidebar.set_roots(roots)
        self.chips.set_roots([(r.id, r.name, r.path) for r in roots])
        self._sync_scope_ui()
        self._update_root_model()
        for r in roots:
            key = f"root:{r.id}"
            if r.state == "denied":
                text = (f"macOS did not allow access to {r.name}." if theme.IS_MAC
                        else f"Access to {r.name} was denied, so it is not indexed.")
                acts = [("Open Privacy Settings", lambda: open_privacy_settings(False))] if theme.IS_MAC else []
                self.notice_bar.post(key, text, acts)
            elif r.state == "missing":
                self.notice_bar.post(key, f"{r.name} was not found. It may be unplugged or moved.",
                                     [("Remove from index…", lambda r=r: self.remove_root(r.id, r.path))])
            else:
                self.notice_bar.remove(key)
        if self._req_empty:
            self._entry_counts()
        self._update_status_bar()
        self._refresh_state()

    def _entry_counts(self) -> None:
        st = self._status
        self.sidebar.set_counts({}, st.total_entries if st is not None else None, "entries")

    def _update_root_model(self) -> None:
        roots = self._roots()
        prefix = self.chips.scope_root_id() is None and not self.chips.chip_state().folder and len(roots) > 1
        self.results.model.set_roots(roots, prefix)

    def _roots(self) -> List[object]:
        return list(self._status.roots) if self._status is not None else []

    def _root(self, root_id: int):
        for r in self._roots():
            if r.id == root_id:
                return r
        return None

    def _configured_roots(self) -> List[str]:
        return [p for p in (getattr(self.config_manager.config, "index_paths", []) or []) if p]

    # ------------------------------------------------------------------ searching
    def _request_key(self, text: str) -> tuple:
        return (text.strip(), self.chips.chip_state(), self.results.sort_spec())

    def _on_search_requested(self, text: str, commit: bool) -> None:
        self._send_search(commit=commit, text=text)

    def _send_search(self, commit: bool = False, text: Optional[str] = None) -> None:
        raw = self.bar.text() if text is None else text
        query = raw.strip()
        key = self._request_key(raw)
        self._req_key = key
        self._req_raw = raw
        self.bar.mark_sent(raw)
        self._idle_commit.stop()
        empty = not query and self.chips.chip_state().is_default()
        self._req_empty = empty
        if empty:
            self._clear_results()
            self._entry_counts()
            self._displayed_key = key
            self.bar.results_text = raw
        if empty:
            # Nothing to list: show the ready state and drop any in-flight result.
            self._gen = -1
            self._pending_send = None
            self._refresh_state()
            return
        if not getattr(self.client, "is_ready", False):
            self._pending_send = commit
            self._refresh_state()
            return
        self.request_count += 1
        self._gen = self.client.search(
            query, self.chips.state(), self.results.sort_spec(),
            limit=200, suggestions=False, commit_history=commit)
        if commit:
            self._committed_key = key
        self._refresh_state()

    def _commit_idle(self) -> None:
        res = self._last_result
        if (res is not None and res.total > 0 and self._displayed_key == self._req_key
                and self._committed_key != self._req_key and not self._req_empty
                and self.bar.text() == self._req_raw):
            self._send_search(commit=True)

    def _on_filters_changed(self, _state: FilterState) -> None:
        self._sync_scope_ui()
        self._update_root_model()
        self._send_search(commit=False)

    def _sync_scope_ui(self) -> None:
        self.sidebar.select_root(self.chips.scope_root_id())
        self.sidebar.set_smart_state(self.chips.smart_state())

    def _on_sidebar_root(self, root_id) -> None:
        self.chips.set_root(root_id)

    def _on_smart_toggled(self, key: str, on: bool) -> None:
        if key in ("image", "document", "code"):
            self.chips.set_kind(key if on else None)
        elif key == "recent":
            self.chips.set_recent(on)
        elif key == "favorites":
            self.chips.set_favorites(on)

    def select_root_index(self, n: int) -> None:
        if n == 0:
            self.chips.set_root(None)
            return
        roots = self._roots()
        if 0 < n <= len(roots):
            self.chips.set_root(roots[n - 1].id)

    def clear_filters(self) -> None:
        self.chips.clear()

    def _fetch_page(self, gen: int, offset: int) -> None:
        if gen != self._gen:
            return
        self.client.fetch_page(gen, offset, PAGE_SIZE)

    def _on_search_result(self, res: SearchResult) -> None:
        t0 = time.perf_counter()
        if res.gen != self._gen:
            current = getattr(self.client, "current_gen", None)
            # A supervisor replay after an engine restart carries the client's newest gen.
            if not (res.gen > self._gen and res.gen == current):
                return
            self._gen = res.gen
        model = self.results.model
        if res.offset > 0 and not res.refresh:
            model.append_page(res)
            self.last_apply_ms = (time.perf_counter() - t0) * 1000
            return
        if self._req_empty:
            self._entry_counts()
            return
        if res.error:
            self.results.set_summary(res.error, error=True)
            self.bar.results_text = self._req_raw
            self.last_apply_ms = (time.perf_counter() - t0) * 1000
            return
        same = (self._displayed_key == self._req_key and model.gen >= 0
                and self.results.is_showing_results())
        if res.refresh or same:
            model.apply_refresh(res)
        else:
            model.set_result(res)
            self.results.view.scrollToTop()
            self.preview.show_row(None, None)
        self._displayed_key = self._req_key
        self._last_result = res
        self.bar.results_text = self._req_raw
        self.results.set_summary(summary_text(res.total, res.elapsed_ms, res.partial))
        counts = {int(rid): int(n) for rid, n in (res.root_counts or ())}
        if counts:
            all_total = sum(counts.values())
        elif self.chips.scope_root_id() is None:
            all_total = res.total
        else:
            all_total = None
        self.sidebar.set_counts(counts, all_total, "results")
        self.preview.set_terms(list(res.terms or ()))
        if res.total > 0 and self._committed_key != self._req_key:
            self._idle_commit.start()
        self._refresh_state()
        self.last_apply_ms = (time.perf_counter() - t0) * 1000

    def _clear_results(self) -> None:
        self._last_result = None
        self.results.model.clear()
        self.results.set_summary("")
        self.preview.show_row(None, None)
        self.bar.results_text = None

    def _on_search_cleared(self) -> None:
        self._send_search(commit=False, text="")
        self._refresh_state()

    def _apply_did_you_mean(self, text: str) -> None:
        self.bar.set_text(text)
        self._send_search(commit=True, text=text)

    # ------------------------------------------------------------------ states (F.9)
    def _first_scan(self) -> Optional[object]:
        st = self._status
        if st is None or st.state != "scanning":
            return None
        for r in st.roots:
            if r.state == "scanning" and r.last_scan_end is None:
                return r
        return None

    def _refresh_state(self) -> None:
        pane = self.results
        st = self._status
        has_roots = bool(self._configured_roots()) or bool(st and st.roots)
        query_active = not self._req_empty
        ready = bool(getattr(self.client, "is_ready", False))
        if not has_roots and ready:
            pane.set_summary("")
            pane.show_empty("no-folders", "No folders are indexed yet",
                            "Add a folder to search the files in it. The index stays on this computer.",
                            [("Add folder…", self.add_folder),
                             ("Index your home folder", lambda: self.add_roots([str(Path.home())]))])
            return
        if self._engine_state == "failed" and not pane.is_showing_results():
            pane.show_empty("engine-failed", "The indexer stopped",
                            "Searching is unavailable until the indexer runs again.",
                            [("Restart", self._restart_engine)])
            return
        if not ready:
            if pane.is_showing_results() and query_active:
                return
            msg = ("Your search runs as soon as the index is loaded." if query_active
                   else "This takes a moment after the app starts.")
            pane.show_empty("loading", "Loading index…", msg)
            return
        if query_active:
            res = self._last_result
            if res is None or self._displayed_key != self._req_key:
                return     # waiting for this request's result; keep what is on screen
            if res.total > 0 or pane.model.rowCount():
                pane.show_results()
                return
            self._show_no_matches(res)
            return
        first = self._first_scan()
        if first is not None:
            pane.set_summary("")
            pane.show_empty("first-scan", f"Indexing {first.name}…",
                            f"{_plural(first.scanned_entries or st.total_entries, 'item')} so far. "
                            "You can search while this runs.")
            return
        total = st.total_entries if st else 0
        folders = len(st.roots) if st else len(self._configured_roots())
        pane.set_summary("")
        pane.show_empty("ready", "Search by name",
                        f"{_plural(total, 'item')} in {_plural(folders, 'folder')} indexed.\n"
                        "Narrow it down with ext:pdf or size:>10mb.")

    def _show_no_matches(self, res: SearchResult) -> None:
        text = self._req_raw.strip()
        title = f"No files match “{display_text(text)}”" if text else "No files match these filters"
        lines = []
        actions = []
        labels = self.chips.active_labels()
        if labels:
            lines.append("Filters are on: " + ", ".join(labels))
            actions.append(("Clear filters", self.clear_filters))
            if self.chips.scope_root_id() is not None or self.chips.chip_state().folder:
                actions.append(("Search all folders", lambda: self.chips.set_root(None)))
        if self._first_scan() is not None or res.partial:
            lines.append("Indexing is still running.")
        if not lines:
            lines.append("Check the spelling or the indexed folders.")
        link = ""
        if res.did_you_mean:
            import html
            safe = html.escape(res.did_you_mean, quote=True)
            link = f'Did you mean <a href="{safe}">{safe}</a>?'
        self.results.show_empty("no-matches", title, "\n".join(lines), actions, link)

    # ------------------------------------------------------------------ status bar (F.7)
    def _update_status_bar(self) -> None:
        st = self._status
        ready = bool(getattr(self.client, "is_ready", False))
        progress = None
        if self._engine_state == "failed" or (st is not None and st.state == "error"):
            dot, text = "error", "Indexer stopped"
        elif self._engine_state == "restarting":
            dot, text = "loading", "Restarting the indexer…"
        elif not ready or st is None or st.state == "loading":
            dot, text = "loading", "Loading index…"
        elif st.state == "scanning":
            dot = "scanning"
            scanning = [r for r in st.roots if r.state == "scanning"]
            if st.message:
                text = st.message
            elif scanning:
                r = scanning[0]
                text = f"Indexing {r.name}… {_plural(r.scanned_entries, 'item')}"
            else:
                text = "Indexing…"
            expected = sum(r.expected_entries for r in scanning)
            done = sum(r.scanned_entries for r in scanning)
            progress = (done, expected)
        elif st.state == "paused":
            dot, text = "paused", st.message or "Index paused (on battery)"
        else:
            dot, text = "active", "Index active"
        self.status_dot.set_state(dot)
        self.status_dot.setAccessibleDescription(text)
        self.status_lbl.setText(text)
        self.count_lbl.setText(f"{_plural(st.total_entries, 'item')} indexed" if st is not None else "")
        if progress is None:
            self.progress_bar.setVisible(False)
        else:
            done, expected = progress
            if expected > 0:
                self.progress_bar.setRange(0, expected)
                self.progress_bar.setValue(min(done, expected))
            else:
                self.progress_bar.setRange(0, 0)
            self.progress_bar.setVisible(True)
        live = st.live if st is not None else ""
        if not live or not ready:
            self.live_lbl.setText("")
            self.live_lbl.setToolTip("")
        elif live == "on":
            self.live_lbl.setText("Live monitoring")
            self.live_lbl.setToolTip("Changes to indexed folders appear in results right away.")
        elif live == "limited":
            self.live_lbl.setText("Live monitoring limited")
            self.live_lbl.setToolTip("The system limit on watched folders (fs.inotify.max_user_watches) "
                                     "was reached. Some changes appear after the next rescan, every 15 minutes.")
        else:
            self.live_lbl.setText("Live monitoring off")
            self.live_lbl.setToolTip("Turned off in Settings, or not available for these folders. "
                                     "Changes appear after the next rescan.")
        self.live_lbl.setAccessibleDescription(self.live_lbl.toolTip())

    # ------------------------------------------------------------------ keyboard
    def _on_nav_key(self, event) -> None:
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self.results.move_selection(event.key(), ctrl)

    def _open_current_or_first(self) -> None:
        if self.results.current_row() is None and self.results.model.rowCount():
            self.results.select_row(0)
        self.open_selected()

    def focus_search(self, query: Optional[str] = None) -> None:
        if self.isHidden() or self.isMinimized():
            self.show_window()
        if query is not None and isinstance(query, str):
            self.bar.set_text(query)
            self.bar.focus()
            self._send_search(commit=True, text=query)
            return
        self.bar.focus()

    def _hide_from_escape(self) -> None:
        if self.tray_icon is not None and theme.setting_bool("close_to_tray", False):
            self.hide()
        else:
            self.showMinimized()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event):  # noqa: N802
        if event.type() == QEvent.Type.PaletteChange:
            self.results.model.palette_changed()
        super().changeEvent(event)

    # ------------------------------------------------------------------ selection & preview
    def _on_current_row(self, current: QModelIndex, _prev=None) -> None:
        row = self.results.model.row(current.row()) if current.isValid() else None
        root = self._root(row.root_id) if row is not None else None
        self.preview.show_row(row, root.path if root is not None else None)

    def _update_item_actions(self) -> None:
        paths = self.results.selected_paths()
        for act in (self.act_open, self.act_copy_path, self.act_copy_name, self.act_trash):
            act.setEnabled(bool(paths))
        for act in (self.act_open_with, self.act_reveal):
            act.setEnabled(len(paths) == 1)
        self.act_export.setEnabled(self.results.model.rowCount() > 0)

    # ------------------------------------------------------------------ file actions
    def _report(self, errors) -> None:
        errors = [e for e in errors if e]
        if errors:
            QMessageBox.warning(self, theme.APP_NAME, "\n\n".join(errors[:5]))

    def _confirm(self, title: str, text: str, info: str, action_text: str,
                 default_to_action: bool = True) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question if default_to_action else QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        box.setInformativeText(info)
        action = box.addButton(action_text, QMessageBox.ButtonRole.AcceptRole if default_to_action
                               else QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(action if default_to_action else cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is action

    def open_paths(self, paths: Sequence[str]) -> None:
        paths = list(paths)
        if not paths:
            return
        if len(paths) > 10 and not self._confirm(
                "Open files", f"Open {len(paths)} items?", "Each one opens in its own app or window.", "Open"):
            return
        self._report(file_actions.open_path(p) for p in paths)
        query = self._req_raw.strip()
        if query and self._committed_key != self._req_key:
            self.client.user_op("record_open", tuple(paths[:1]), query)
            self._committed_key = self._req_key

    def open_selected(self) -> None:
        self.open_paths(self.results.selected_paths())

    def reveal_selected_path(self) -> None:
        row = self.results.current_row()
        if row is not None:
            self._report([file_actions.reveal(row.path)])

    def _open_with_selected(self, path: Optional[str] = None, application: Optional[str] = None) -> None:
        if path is None:
            row = self.results.current_row()
            path = row.path if row is not None else None
        if not path:
            return
        if file_actions.open_with_mode() == "choose-app" and not application:
            application, _ = QFileDialog.getOpenFileName(self, "Choose an app", "/Applications", "Apps (*.app)")
            if not application:
                return
        self._report([file_actions.open_with(path, application)])

    def copy_paths(self, paths: Sequence[str]) -> None:
        if paths:
            QGuiApplication.clipboard().setText("\n".join(paths))
            self.statusBar().showMessage("Copied path" if len(paths) == 1 else f"Copied {len(paths)} paths", 4000)

    def copy_names(self, paths: Sequence[str]) -> None:
        if paths:
            QGuiApplication.clipboard().setText("\n".join(os.path.basename(p) for p in paths))
            self.statusBar().showMessage("Copied name" if len(paths) == 1 else f"Copied {len(paths)} names", 4000)

    def copy_selected_path(self) -> None:
        self.copy_paths(self.results.selected_paths())

    def copy_selected_name(self) -> None:
        self.copy_names(self.results.selected_paths())

    def toggle_favorite(self, path: str, on: bool) -> None:
        self.client.user_op("favorite" if on else "unfavorite", (path,))
        self.results.model.set_favorite(path, on)

    def _edit_info(self, what: str, path: str) -> None:
        req = self.client.request_item_info(path)
        self._pending_info[req] = (what, path)

    def _on_item_info(self, info) -> None:
        pending = self._pending_info.pop(getattr(info, "req_id", -1), None)
        if pending is None:
            return
        what, path = pending
        name = display_text(os.path.basename(path))
        if what == "tags":
            text, ok = QInputDialog.getText(self, "Tags", f"Tags for “{name}”, separated by commas:",
                                            QLineEdit.EchoMode.Normal, ", ".join(info.tags))
            if ok:
                tags = [t.strip() for t in text.split(",") if t.strip()]
                self.client.user_op("set_tags", (path,), tags)
        else:
            text, ok = QInputDialog.getMultiLineText(self, "Note", f"Note for “{name}”:", info.note)
            if ok:
                self.client.user_op("set_note", (path,), text.strip())

    def trash_selected(self) -> None:
        self.trash_paths(self.results.selected_paths())

    def trash_paths(self, paths: Sequence[str]) -> None:
        """Move to the Recycle Bin/Trash after confirming. Permanent deletion only when
        the location has no trash, and only after a second, explicit confirmation."""
        paths = list(paths)
        if not paths:
            return
        n = len(paths)
        what = f"“{display_text(os.path.basename(paths[0]))}”" if n == 1 else f"{n} items"
        if not self._confirm(theme.trash_label(), f"Move {what} to {theme.trash_name()}?",
                             f"You can restore {'it' if n == 1 else 'them'} from {theme.trash_name()}.",
                             theme.trash_label()):
            return
        moved, no_trash, errors = [], [], []
        for p in paths:
            ok, err = file_actions.move_to_trash(p)
            if ok:
                moved.append(p)
            elif err:
                errors.append(err)
            else:
                no_trash.append(p)
        deleted = []
        if no_trash:
            m = len(no_trash)
            what = f"“{display_text(os.path.basename(no_trash[0]))}”" if m == 1 else f"{m} items"
            if self._confirm("Delete permanently", f"{what} can’t be moved to {theme.trash_name()}.",
                             f"Delete {'it' if m == 1 else 'them'} permanently? This can’t be undone.",
                             "Delete permanently", default_to_action=False):
                for p in no_trash:
                    err = file_actions.delete_permanently(p)
                    if err:
                        errors.append(err)
                    else:
                        deleted.append(p)
        gone = moved + deleted
        if gone:
            self.results.model.remove_paths(gone)
            self.client.forget(gone)
            k = len(gone)
            self.statusBar().showMessage(
                f"Moved {_plural(k, 'item')} to {theme.trash_name()}" if not deleted
                else f"Deleted {_plural(k, 'item')}", 4000)
        self._report(errors)

    def build_context_menu(self, rows) -> QMenu:
        menu = QMenu(self)
        paths = [r.path for r in rows]
        path = paths[0]
        many = len(paths) > 1
        menu.addAction("Open", lambda: self.open_paths(paths))
        mode = file_actions.open_with_mode()
        if not many and mode in ("system", "choose-app"):
            menu.addAction("Open with…", lambda: self._open_with_selected(path))
        elif not many and mode == "list":
            sub = menu.addMenu("Open with")
            sub.aboutToShow.connect(lambda: self._fill_open_with(sub, path))
        reveal = menu.addAction(theme.reveal_label(), lambda: self._report([file_actions.reveal(path)]))
        reveal.setEnabled(not many)
        menu.addSeparator()
        menu.addAction("Copy paths" if many else "Copy path", lambda: self.copy_paths(paths))
        menu.addAction("Copy names" if many else "Copy name", lambda: self.copy_names(paths))
        if not many:
            menu.addSeparator()
            fav = bool(rows[0].favorite)
            menu.addAction("Remove from favorites" if fav else "Add to favorites",
                           lambda: self.toggle_favorite(path, not fav))
            menu.addAction("Edit tags…", lambda: self._edit_info("tags", path))
            menu.addAction("Edit note…", lambda: self._edit_info("note", path))
        menu.addSeparator()
        menu.addAction(theme.trash_label(), lambda: self.trash_paths(paths))
        return menu

    def _fill_open_with(self, menu: QMenu, path: str) -> None:
        if menu.actions():
            return
        choices = file_actions.open_with_choices(path)
        if not choices:
            menu.addAction("No apps found").setEnabled(False)
        for name, app in choices:
            menu.addAction(name, lambda a=app: self._open_with_selected(path, a))

    def _results_context_menu(self, pos) -> None:
        view = self.results.view
        index = view.indexAt(pos)
        if not index.isValid():
            return
        if not view.selectionModel().isRowSelected(index.row(), QModelIndex()):
            view.setCurrentIndex(index)
        rows = self.results.selected_rows()
        if rows:
            self._last_menu = self.build_context_menu(rows)
            self._last_menu.popup(view.viewport().mapToGlobal(pos))

    def export_results(self) -> None:
        model = self.results.model
        rows = [{"name": r.name, "folder": os.path.dirname(r.path), "size": r.size, "modified": r.mtime,
                 "path": r.path, "kind": kinds.kind_label(r.name, r.flags)} for r in model.rows]
        msg = export_results(self, rows)
        if msg and len(model.rows) < model.total:
            msg += f" Scroll further to load the other {format_count(model.total - len(model.rows))}."
        if msg:
            self.statusBar().showMessage(msg, 6000)

    # ------------------------------------------------------------------ folders & settings
    def _engine_config(self) -> EngineConfig:
        cfg = self.config_manager.config
        to_ec = getattr(cfg, "to_engine_config", None)
        if to_ec is not None:
            return to_ec()
        return EngineConfig(roots=tuple(cfg.index_paths), exclude_patterns=tuple(cfg.exclude_patterns))

    def _configure(self) -> None:
        self.client.configure(self._engine_config())
        self._refresh_state()

    def _save_config(self) -> bool:
        try:
            self.config_manager.save()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "Settings not saved", f"The settings could not be saved.\n\n{exc}")
            return False

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add folder to index")
        if folder:
            self.add_roots([os.path.normpath(folder)])

    def add_roots(self, folders: Sequence[str]) -> None:
        cfg = self.config_manager.config
        changed = False
        for f in folders:
            f = os.path.normpath(f)
            if not os.path.isdir(f):
                continue
            if any(paths_equal(f, p) or is_path_under(f, p) for p in cfg.index_paths):
                continue
            cfg.index_paths.append(f)
            changed = True
        if changed and self._save_config():
            self._configure()

    def remove_root(self, root_id: int, path: str) -> None:
        name = display_text(os.path.basename(os.path.normpath(path)) or path)
        if not self._confirm("Remove from index", f"Stop indexing “{name}”?",
                             "Its files are removed from the index. Nothing is deleted from the disk.",
                             "Remove"):
            return
        cfg = self.config_manager.config
        cfg.index_paths = [p for p in cfg.index_paths if not paths_equal(p, path)]
        if self.chips.root_id() == root_id:
            self.chips.set_root(None)
        if self._save_config():
            self._configure()

    def exclude_subfolder(self, root_id: int, root_path: str) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Exclude a subfolder", root_path)
        if not folder:
            return
        folder = os.path.normpath(folder)
        if not is_path_under(folder, root_path) or paths_equal(folder, root_path):
            QMessageBox.information(self, "Exclude a subfolder", "Choose a folder inside the indexed folder.")
            return
        parent = os.path.dirname(os.path.normpath(root_path))
        pattern = os.path.relpath(folder, parent).replace(os.sep, "/")
        cfg = self.config_manager.config
        if pattern not in cfg.exclude_patterns:
            cfg.exclude_patterns.append(pattern)
            if self._save_config():
                self._configure()

    def update_index(self) -> None:
        self.client.index("update")

    def rebuild_index(self) -> None:
        if self._confirm("Rebuild index", "Rebuild the index?",
                         "Every folder is read again. This can take a while for large folders.", "Rebuild"):
            self.client.index("rebuild")

    def open_settings(self) -> None:
        states = {r.path: (r.state, r.entries) for r in self._roots()}
        dialog = SettingsDialog(self.config_manager, self, root_states=states,
                                on_rebuild=self.rebuild_index, on_show_logs=self.show_log_folder)
        self._settings_dialog = dialog
        if dialog.exec():
            self.set_appearance(dialog.appearance)
            if dialog.indexing_changed:
                self._configure()
        self._settings_dialog = None

    def show_onboarding(self) -> None:
        dlg = OnboardingDialog(self, preselected=self._configured_roots())
        self.onboarding = dlg

        def done(result: int) -> None:
            if result:
                self.add_roots(dlg.folders())
            self.onboarding = None
            self._refresh_state()

        dlg.finished.connect(done)
        dlg.open()

    def show_log_folder(self) -> None:
        d = log_dir()
        if d is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def diagnostics_text(self) -> str:
        from PySide6 import __version__ as pyside
        from PySide6.QtCore import qVersion

        lines = [f"{APP_NAME} {app_version()}",
                 f"Python {platform.python_version()}, Qt {qVersion()}, PySide {pyside}",
                 f"OS {platform.platform()} ({platform.machine()})",
                 f"Frozen: {bool(getattr(sys, 'frozen', False) or '__compiled__' in globals())}",
                 f"Engine: {self._engine_state}"]
        st = self._status
        if st is not None:
            lines.append(f"Index: {st.state}, {st.total_entries} entries, {st.total_folders} folders, "
                         f"live {st.live}, generation {st.index_generation}")
            for r in st.roots:
                lines.append(f"  {r.path}: {r.state}, {r.entries} entries, {r.denied_dirs} denied dirs, live {r.live}")
        try:
            from .. import applog

            lines += ["", "Last log lines:", applog.tail_log(50)]
        except Exception:
            pass
        return "\n".join(lines)

    def copy_diagnostics(self) -> None:
        QGuiApplication.clipboard().setText(self.diagnostics_text())
        self.statusBar().showMessage("Copied diagnostics", 4000)

    def open_about(self) -> None:
        AboutDialog(self).exec()

    # ------------------------------------------------------------------ tray & lifetime
    def setup_system_tray(self) -> None:
        self.tray_icon: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self)
        tray.setToolTip(APP_NAME)
        icon = app_icon()
        tray.setIcon(icon if not icon.isNull() else self.style().standardIcon(
            self.style().StandardPixmap.SP_FileDialogContentsView))
        menu = QMenu(self)
        menu.addAction("Show window", self.show_window)
        menu.addAction("Find…", self.focus_search)
        menu.addAction(self.act_update_index)
        menu.addAction(self.act_settings)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray_icon = tray

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger and not theme.IS_MAC:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()
                self.focus_search()

    def close_and_quit(self) -> None:
        self._shutdown()
        QApplication.instance().quit()

    def _shutdown(self) -> None:
        """Save the layout and stop the preview worker. Idempotent. The engine client
        is shut down by ``main`` on ``aboutToQuit``."""
        if self._shutdown_done:
            return
        try:
            self._save_ui_state()
        except Exception:
            pass
        self._shutdown_done = True
        self._idle_commit.stop()
        self.bar.timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        try:
            self.preview.shutdown()
        except Exception:
            pass
        if self.tray_icon is not None:
            self.tray_icon.hide()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if (self.tray_icon is not None and not self._shutdown_done
                and theme.setting_bool("close_to_tray", False)):
            self._save_ui_state()
            self.hide()
            event.ignore()
            return
        self._shutdown()
        event.accept()
