from __future__ import annotations

import os
import re
from typing import List, Optional

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QLabel, QMainWindow, QMenu, QMessageBox, QProgressBar, QSplitter,
    QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget,
)

from ..config import AppConfig, config_manager
from ..file_watcher import FileWatcher
from ..indexer import IndexStats, Indexer
from ..persistent_index import PersistentIndex
from ..search import SearchEngine
from ..search_history import SearchHistory
from ..utils.file_utils import ExcludeMatcher
from ..utils.platform_utils import get_default_index_root, paths_equal
from . import file_actions, theme
from .browser_view import BrowserView
from .search_view import SearchView
from .theme import APP_NAME, SPACE_M, SPACE_S, display_text, format_count
from .widgets import AboutDialog, FilterPanel, PreviewPanel, SettingsDialog, app_icon, export_results


class IndexerThread(QThread):
    """Brings the persistent index up to date in the background.

    By default it performs an *incremental* update (only new/changed/deleted entries
    are touched). ``force_rebuild=True`` re-reads every entry of the given roots.
    """
    progress = Signal(int, int)  # processed, total (total == 0: unknown)
    root_started = Signal(str)   # the indexed folder now being scanned
    done = Signal(object)        # IndexStats
    error = Signal(str)

    def __init__(
        self,
        index_paths: List[str],
        persistent_index: PersistentIndex,
        exclude_patterns: List[str],
        force_rebuild: bool = False,
        index_content: bool = False,
    ):
        super().__init__()
        self.index_paths = list(index_paths)
        self.persistent_index = persistent_index
        self.exclude_patterns = list(exclude_patterns)
        self.force_rebuild = force_rebuild

        cfg = AppConfig()
        cfg.index_paths = self.index_paths
        cfg.exclude_patterns = self.exclude_patterns
        cfg.index_content = index_content
        # Created up front so stop() can never race with thread start-up.
        self.indexer = Indexer(config=cfg, persistent_index=persistent_index)
        self.indexer.on_progress(lambda p, t: self.progress.emit(p, t))

    def run(self):
        try:
            combined = IndexStats()
            succeeded = 0
            failures: List[str] = []
            for path in self.index_paths:
                if self.indexer.stopped:
                    break
                self.root_started.emit(path)
                try:
                    if self.force_rebuild:
                        stats = self.indexer.index_path(path)
                    else:
                        stats = self.indexer.incremental_update(path)
                    combined.merge(stats)
                    succeeded += 1
                except Exception as e:  # one bad/unplugged root must not stop the others
                    failures.append(f"{path}: {e}")
            if failures and succeeded == 0 and not self.indexer.stopped:
                self.error.emit("; ".join(failures))
            else:
                combined.errors += len(failures)
                self.done.emit(combined)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            try:
                self.persistent_index.release_thread_connection()
            except Exception:
                pass

    def stop(self):
        self.indexer.stop()


class SearchWorker(QThread):
    """Runs a search off the GUI thread."""
    results_ready = Signal(object, str)  # SearchResponse, raw_query
    error = Signal(str)

    def __init__(self, search_engine, raw_query: str, extended_query: str, filters: dict,
                 max_results: int, index_paths: list):
        super().__init__()
        self.search_engine = search_engine
        self.raw_query = raw_query
        self.extended_query = extended_query
        self.filters = filters
        self.max_results = max_results
        self.index_paths = index_paths

    def run(self):
        try:
            try:
                regex = re.compile(self.filters["regex"], re.IGNORECASE) if self.filters["regex"] else None
            except re.error as exc:
                self.error.emit(f"The path filter is not a valid regular expression ({exc}).")
                return
            excludes = ExcludeMatcher(self.filters["exclude_patterns"])

            resp = self.search_engine.search_persistent(self.extended_query, page_size=self.max_results)
            if getattr(resp, "error", None):
                self.error.emit(str(resp.error))
                return

            # Nothing in the index: try a live walk of the folders (plain substring only,
            # so skip it for regex/wildcard/operator queries).
            plain = not any(c in self.extended_query for c in "*?:\"") and not self.extended_query.lower().startswith("re:")
            if resp.total_count == 0 and self.index_paths and plain:
                resp = self._filesystem_fallback_search(self.raw_query, self.max_results)

            filtered_results = []
            for r in resp.results:
                if regex is not None and not regex.search(r.path):
                    continue
                if excludes and excludes.matches_path(r.path):
                    continue
                if self.filters["date_range"]:
                    lo, hi = self.filters["date_range"]
                    if lo is not None and r.modified < lo:
                        continue
                    if hi is not None and r.modified > hi:
                        continue
                filtered_results.append(r)

            resp.results = filtered_results
            resp.total_count = len(filtered_results)
            self.results_ready.emit(resp, self.raw_query)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            index = getattr(self.search_engine, "index", None)
            if index is not None and hasattr(index, "release_thread_connection"):
                index.release_thread_connection()

    _FALLBACK_TIME_LIMIT = 5.0  # seconds; the live walk is only a best-effort fallback

    def _filesystem_fallback_search(self, query: str, max_results: int = 500):
        from ..search import SearchResponse, SearchResult
        import time as _time

        start = _time.time()
        query_lower = query.lower()
        results = []
        excludes = ExcludeMatcher(self.filters["exclude_patterns"])

        for root_path in self.index_paths:
            if not os.path.isdir(root_path):
                continue
            try:
                for dirpath, dirnames, filenames in os.walk(root_path):
                    if self.isInterruptionRequested() or _time.time() - start > self._FALLBACK_TIME_LIMIT:
                        break
                    dirnames[:] = [d for d in dirnames if not excludes.matches_name(d)]
                    for name in filenames + dirnames:
                        if query_lower in name.lower():
                            full_path = os.path.join(dirpath, name)
                            try:
                                st = os.stat(full_path)
                                is_dir = os.path.isdir(full_path)
                                ext = os.path.splitext(name)[1].lower() if not is_dir else ""
                                score = 20.0 if name.lower() == query_lower else (10.0 if name.lower().startswith(query_lower) else 5.0)
                                results.append(SearchResult(
                                    path=full_path, name=name, extension=ext, size=st.st_size,
                                    modified=int(st.st_mtime), is_folder=is_dir, score=score,
                                    matches=[query],
                                ))
                            except (OSError, PermissionError):
                                continue
                    if len(results) >= max_results:
                        break
            except (OSError, PermissionError):
                continue
            if len(results) >= max_results:
                break

        results.sort(key=lambda r: (-r.score, r.name.lower()))
        elapsed = (_time.time() - start) * 1000
        return SearchResponse(
            results=results[:max_results], total_count=len(results), page=1,
            page_size=max_results, total_pages=1,
            query_time_ms=-elapsed,  # negative: came from the live walk
        )


def _format_ms(ms: float) -> str:
    if ms < 10:
        return f"{ms:.1f} ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.1f} s"


def _plural(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{format_count(n)} {word if n == 1 else (plural or word + 's')}"


class MainWindow(QMainWindow):
    """Search field on top; folders and filters, results, and preview below."""
    # Emitted from the file-watcher's worker threads; Qt delivers them on the GUI thread.
    watcher_event = Signal(str, str)
    watcher_error = Signal(str)

    def __init__(self):
        super().__init__()
        self._shutdown_done = False
        self.setWindowTitle(APP_NAME)
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        theme.apply_appearance(theme.current_appearance())

        # Data & core
        self.db_index = PersistentIndex()
        self.search_history = SearchHistory()
        self.search_engine = SearchEngine(persistent_index=self.db_index)
        self.file_watcher = FileWatcher(persistent_index=self.db_index, config=config_manager.config)

        self.indexer_thread: Optional[IndexerThread] = None
        self._pending_index_jobs: List[tuple] = []
        self.search_worker: Optional[SearchWorker] = None
        self._stale_search_workers: List[SearchWorker] = []
        self._index_root = ""
        self._index_processed = 0
        self._indexed_count = 0
        self._last_index_message = ""

        self.watcher_event.connect(self.on_file_watcher_change)
        self.watcher_error.connect(self._on_watcher_error)
        self.file_watcher.on_change(lambda t, p: self.watcher_event.emit(t, p))
        self.file_watcher.on_error(lambda exc: self.watcher_error.emit(str(exc)))
        app = QApplication.instance()
        if app is not None:
            # Also covers quitting from the tray menu or the macOS app menu.
            app.aboutToQuit.connect(self._shutdown)

        self._build_ui()
        self._build_actions()
        self._build_menus()
        self.setup_system_tray()
        self._restore_ui_state()

        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(2000)
        self._count_timer.timeout.connect(self._update_index_count)

        self._update_index_count()
        self.apply_initial_configurations()
        self.start_file_watchers()
        self.update_extensions_dropdown()
        self._update_item_actions()
        self.search_view.search_input.setFocus()

    # ------------------------------------------------------------------ layout
    def _build_ui(self) -> None:
        central = QWidget()
        self.central_widget = central
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(SPACE_M, SPACE_M, SPACE_M, SPACE_S)
        layout.setSpacing(SPACE_M)

        self.search_view = SearchView(history_mgr=self.search_history, persistent_index=self.db_index)
        self.filters_btn = QToolButton()
        self.filters_btn.setText("Filters")
        self.filters_btn.setCheckable(True)
        self.filters_btn.setToolTip("Show or hide the filters")
        self.filters_btn.setAccessibleName("Filters")
        self.search_view.search_row.addWidget(self.filters_btn)
        layout.addWidget(self.search_view.search_bar)

        self.browser_view = BrowserView()
        self.browser_view.path_changed.connect(self.on_browser_path_changed)
        self.browser_view.folders_dropped.connect(self.on_folders_dropped)
        self.browser_view.reindex_folder.connect(self.startIndexingFolder)
        self.browser_view.select_folder.connect(self.on_folder_selected)

        self.filter_panel = FilterPanel()
        self.filter_panel.filters_changed.connect(self._on_filters_changed)

        self.sidebar = QSplitter(Qt.Orientation.Vertical)
        self.sidebar.setChildrenCollapsible(False)
        self.sidebar.setHandleWidth(SPACE_M)
        self.sidebar.addWidget(self.browser_view)
        self.sidebar.addWidget(self.filter_panel)
        self.sidebar.setStretchFactor(0, 1)
        self.sidebar.setStretchFactor(1, 0)
        self.left_widget = self.sidebar

        self.search_view.search_triggered.connect(self.run_search)
        self.search_view.file_selected.connect(self.on_file_selected)
        self.search_view.status_message.connect(lambda m: self.statusBar().showMessage(m, 4000))
        self.search_view.cleared.connect(self._on_search_cleared)
        self.search_view.hide_requested.connect(self._hide_from_escape)
        self.search_view.selection_changed.connect(self._update_item_actions)

        self.preview_panel = PreviewPanel()

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(SPACE_M)
        self.main_splitter.addWidget(self.sidebar)
        self.main_splitter.addWidget(self.search_view)
        self.main_splitter.addWidget(self.preview_panel)
        self.main_splitter.setCollapsible(1, False)
        for i, stretch in enumerate((0, 1, 0)):
            self.main_splitter.setStretchFactor(i, stretch)
        self.main_splitter.setSizes([260, 620, 300])
        layout.addWidget(self.main_splitter, 1)

        # Status bar: message on the left; index size and progress on the right.
        status = self.statusBar()
        self.status_bar = status
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setTextFormat(Qt.TextFormat.PlainText)
        # Line the status text up with the window content above it.
        self.status_lbl.setContentsMargins(SPACE_M - 2, 0, 0, 0)
        status.addWidget(self.status_lbl, 1)
        self.index_lbl = QLabel("")
        self.index_lbl.setContentsMargins(0, 0, SPACE_S, 0)
        status.addPermanentWidget(self.index_lbl)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(120)
        self.progress_bar.setMaximumHeight(self.fontMetrics().height())
        self.progress_bar.setAccessibleName("Indexing progress")
        status.addPermanentWidget(self.progress_bar)

        QWidget.setTabOrder(self.search_view.search_input, self.search_view.fav_btn)
        QWidget.setTabOrder(self.search_view.fav_btn, self.filters_btn)
        QWidget.setTabOrder(self.filters_btn, self.search_view.results_view)
        QWidget.setTabOrder(self.search_view.results_view, self.preview_panel.tabs)

    # ------------------------------------------------------------------ actions & menus
    def _action(self, text: str, slot=None, shortcuts=(), checkable: bool = False,
                role: Optional[QAction.MenuRole] = None) -> QAction:
        act = QAction(text, self)
        keys: List[QKeySequence] = []
        for s in shortcuts:
            keys.extend(QKeySequence.keyBindings(s) if isinstance(s, QKeySequence.StandardKey)
                        else [QKeySequence(s)])
        keys = [k for k in keys if not k.isEmpty()]
        if keys:
            act.setShortcuts(keys)
        act.setCheckable(checkable)
        if role is not None:
            act.setMenuRole(role)
        else:
            act.setMenuRole(QAction.MenuRole.NoRole)
        if slot is not None:
            act.triggered.connect(lambda checked=False, s=slot: s(checked) if checkable else s())
        self.addAction(act)  # shortcuts work even when the menu bar is hidden (Linux/KDE)
        return act

    def _build_actions(self) -> None:
        SK = QKeySequence.StandardKey
        ctrl = Qt.KeyboardModifier.ControlModifier
        self.act_open = self._action("Open", self.open_selected_file, [SK.Open])
        self.act_open_with = self._action("Open with…", self._open_with_selected)
        self.act_open_with.setVisible(file_actions.open_with_mode() in ("system", "choose-app"))
        self.act_reveal = self._action(theme.reveal_label(), self.reveal_selected_path, [
            QKeySequence(ctrl | Qt.Key.Key_Return), QKeySequence(ctrl | Qt.Key.Key_Enter)])
        trash_keys = [SK.Delete]
        if theme.IS_MAC:
            trash_keys.append(QKeySequence(ctrl | Qt.Key.Key_Backspace))
        self.act_trash = self._action(theme.trash_label(), self.delete_selected_path, trash_keys)
        self.act_add_folder = self._action("Add folder…", self.add_folder)
        self.act_export = self._action("Export results…", self.export_results)
        self.act_settings = self._action(f"{theme.settings_label()}…", self.open_settings,
                                         [SK.Preferences], role=QAction.MenuRole.PreferencesRole)
        self.act_quit = self._action("Exit" if theme.IS_WINDOWS else "Quit", self.close_and_quit,
                                     [SK.Quit], role=QAction.MenuRole.QuitRole)

        self.act_copy_path = self._action("Copy path", self.copy_selected_path, [SK.Copy])
        self.act_copy_name = self._action("Copy name", self.copy_selected_name,
                                          [QKeySequence(ctrl | Qt.KeyboardModifier.ShiftModifier | Qt.Key.Key_C)])
        self.act_find = self._action("Find", self.focus_search,
                                     [SK.Find, QKeySequence(ctrl | Qt.Key.Key_L)])

        self.act_filters = self._action("Filters", self.toggle_filter_panel,
                                        [QKeySequence(ctrl | Qt.KeyboardModifier.ShiftModifier | Qt.Key.Key_F)],
                                        checkable=True)
        self.filters_btn.toggled.connect(self.toggle_filter_panel)
        self.act_folders = self._action("Folders", self._set_folders_visible, checkable=True)
        self.act_preview = self._action("Preview", self._set_preview_visible, checkable=True)
        self.appearance_group = QActionGroup(self)
        self.appearance_actions = {}
        for mode, text in (("system", "Use system setting"), ("light", "Light"), ("dark", "Dark")):
            act = QAction(text, self, checkable=True)
            act.setMenuRole(QAction.MenuRole.NoRole)
            act.triggered.connect(lambda _=False, m=mode: self.set_appearance(m))
            self.appearance_group.addAction(act)
            self.appearance_actions[mode] = act
        self.appearance_actions.get(theme.current_appearance(), self.appearance_actions["system"]).setChecked(True)

        self.act_update_index = self._action("Update index", self.startIndexing, [SK.Refresh])
        self.act_rebuild_index = self._action("Rebuild index…", self.rebuild_index)
        self.act_about = self._action(f"About {APP_NAME}", self.open_about,
                                      role=QAction.MenuRole.AboutRole)

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

        view_menu = bar.addMenu("&View")
        view_menu.addAction(self.act_folders)
        view_menu.addAction(self.act_filters)
        view_menu.addAction(self.act_preview)
        view_menu.addSeparator()
        appearance = view_menu.addMenu("Appearance")
        for act in self.appearance_actions.values():
            appearance.addAction(act)

        index_menu = bar.addMenu("&Index")
        index_menu.addAction(self.act_update_index)
        index_menu.addAction(self.act_rebuild_index)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.act_about)

    # ------------------------------------------------------------------ ui state
    def _restore_ui_state(self) -> None:
        s = theme.ui_settings()
        geometry = s.value("window/geometry")
        if not geometry or not self.restoreGeometry(geometry):
            self.resize(1180, 760)
        for key, splitter in (("window/main_splitter", self.main_splitter),
                              ("window/sidebar_splitter", self.sidebar)):
            state = s.value(key)
            if state:
                splitter.restoreState(state)
        self._set_folders_visible(theme.setting_bool("view/folders", True))
        self.toggle_filter_panel(theme.setting_bool("view/filters", True))
        self._set_preview_visible(theme.setting_bool("view/preview", True))

    def _save_ui_state(self) -> None:
        s = theme.ui_settings()
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/main_splitter", self.main_splitter.saveState())
        s.setValue("window/sidebar_splitter", self.sidebar.saveState())
        s.setValue("view/folders", self.act_folders.isChecked())
        s.setValue("view/filters", self.act_filters.isChecked())
        s.setValue("view/preview", self.act_preview.isChecked())
        self.search_view.save_state()
        s.sync()

    def toggle_filter_panel(self, visible: Optional[bool] = None):
        if visible is None or not isinstance(visible, bool):
            visible = not self.filter_panel.isVisibleTo(self)
        self.filter_panel.setVisible(visible)
        for w in (self.act_filters, self.filters_btn):
            w.blockSignals(True)
            w.setChecked(visible)
            w.blockSignals(False)
        self._sync_sidebar()

    def _set_folders_visible(self, visible: bool) -> None:
        self.browser_view.setVisible(bool(visible))
        self.act_folders.setChecked(bool(visible))
        self._sync_sidebar()

    def _set_preview_visible(self, visible: bool) -> None:
        self.preview_panel.setVisible(bool(visible))
        self.act_preview.setChecked(bool(visible))

    def _sync_sidebar(self) -> None:
        if hasattr(self, "act_folders"):
            self.sidebar.setVisible(self.act_folders.isChecked() or self.act_filters.isChecked())
        n = self.filter_panel.active_filter_count()
        self.filters_btn.setText(f"Filters ({n})" if n else "Filters")

    def set_appearance(self, mode: str) -> None:
        theme.apply_appearance(mode)
        theme.ui_settings().setValue("appearance", mode)
        act = self.appearance_actions.get(mode)
        if act is not None:
            act.setChecked(True)

    # ------------------------------------------------------------------ indexing
    def apply_initial_configurations(self):
        paths = [p for p in config_manager.config.index_paths if p]
        existing = [p for p in paths if os.path.isdir(p)]
        self.browser_view.set_root_path(existing[0] if existing else get_default_index_root())
        if paths:
            self.startIndexing()
        self._refresh_empty_state()

    def startIndexing(self, force: bool = False):  # noqa: N802 (public name kept)
        """Bring the index up to date. Normally incremental (reuses the index stored on
        disk); ``force=True`` re-reads every entry of every folder."""
        paths = list(config_manager.config.index_paths)
        if not paths:
            self._set_status("No folders are indexed. Add a folder to start.")
            self._refresh_empty_state()
            return
        force = force is True  # ignore stray values passed by Qt signals
        self._launch_indexer(paths, force)

    def startIndexingFolder(self, folder_path: str):  # noqa: N802
        """Re-read a single folder."""
        self._launch_indexer([folder_path], True)

    def _launch_indexer(self, paths: List[str], force: bool):
        if self.indexer_thread is not None and self.indexer_thread.isRunning():
            job = (tuple(paths), force)
            if job not in self._pending_index_jobs:
                self._pending_index_jobs.append(job)
            self._set_status("Indexing is already running. The update will start when it finishes.")
            return

        self._index_root = paths[0]
        self._index_processed = 0
        self._set_status(f"{'Rebuilding' if force else 'Updating'} index for "
                         f"“{self._folder_name(paths[0])}”…")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.act_update_index.setEnabled(False)
        self.act_rebuild_index.setEnabled(False)

        thread = IndexerThread(
            paths, self.db_index, config_manager.config.exclude_patterns,
            force_rebuild=force, index_content=config_manager.config.index_content,
        )
        thread.progress.connect(self.on_indexer_progress)
        thread.root_started.connect(self._on_index_root_started)
        thread.done.connect(self.on_indexer_finished)
        thread.error.connect(self.on_indexer_error)
        thread.finished.connect(self._on_indexer_thread_finished)
        self.indexer_thread = thread
        thread.start()
        self._refresh_empty_state()

    def is_indexing(self) -> bool:
        return self.indexer_thread is not None and self.indexer_thread.isRunning()

    def _on_indexer_thread_finished(self):
        self.progress_bar.setVisible(False)
        self.act_update_index.setEnabled(True)
        self.act_rebuild_index.setEnabled(True)
        if self._shutdown_done or not self._pending_index_jobs:
            self._refresh_empty_state()
            return
        paths, force = self._pending_index_jobs.pop(0)
        self._launch_indexer(list(paths), force)

    @staticmethod
    def _folder_name(path: str) -> str:
        return display_text(os.path.basename(os.path.normpath(path)) or path)

    @Slot(str)
    def _on_index_root_started(self, root: str):
        self._index_root = root
        self.status_lbl.setToolTip(display_text(root))

    @Slot(int, int)
    def on_indexer_progress(self, processed, total):
        self._index_processed = processed
        name = self._folder_name(self._index_root) if self._index_root else "folders"
        if total > 0:
            self._set_status(f"Indexing “{name}”… {format_count(processed)} of {format_count(total)} items")
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(processed, total))
        else:  # first scan: the total isn't known yet
            self._set_status(f"Indexing “{name}”… {_plural(processed, 'item')} found")
            self.progress_bar.setRange(0, 0)
        if self.search_view.empty_state.kind == "indexing":
            self._refresh_empty_state()

    @Slot(object)
    def on_indexer_finished(self, stats: Optional[IndexStats]):
        self.progress_bar.setVisible(False)
        self.status_lbl.setToolTip("")
        self._update_index_count()
        if stats:
            total = stats.total_files + stats.total_folders
            msg = (f"Index up to date: {_plural(total, 'item')} scanned, {format_count(stats.added)} new, "
                   f"{format_count(stats.updated)} changed, {format_count(stats.removed)} removed "
                   f"({stats.duration:.1f} s)")
            if stats.errors:
                msg += f". {_plural(stats.errors, 'item')} could not be read"
            self._set_status(msg)
        else:
            self._set_status("Index up to date")
        self.update_extensions_dropdown()
        self.browser_view.tree_view.viewport().update()

    @Slot(str)
    def on_indexer_error(self, err_msg):
        self.progress_bar.setVisible(False)
        self._set_status(f"Indexing failed: {err_msg}")
        self.status_lbl.setToolTip(err_msg)

    def rebuild_index(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Rebuild index")
        box.setText("Rebuild the index?")
        box.setInformativeText("Every file in the indexed folders is read again. "
                               "This can take a while for large folders.")
        go = box.addButton("Rebuild", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(go)
        box.exec()
        if box.clickedButton() is go:
            self.startIndexing(force=True)

    def update_extensions_dropdown(self):
        try:
            self.filter_panel.update_extensions(self.db_index.get_top_extensions(15))
        except Exception:
            pass

    def _update_index_count(self) -> None:
        try:
            stats = self.db_index.get_index_stats()
            self._indexed_count = int(stats.get("file_count", 0) or 0)
        except Exception:
            return
        n = len(config_manager.config.index_paths)
        self.index_lbl.setText(f"{_plural(self._indexed_count, 'item')} indexed" if n else "")
        if self.search_view.empty_state.kind in ("ready", "indexing"):
            self._refresh_empty_state()

    # ------------------------------------------------------------------ watcher
    def start_file_watchers(self):
        try:
            self.file_watcher.stop()
            self.file_watcher.set_paths(config_manager.config.index_paths)
            self.file_watcher.start()
        except Exception as e:
            self._set_status(f"Live updates are off: {e}")

    @Slot(str, str)
    def on_file_watcher_change(self, event_type: str, path: str):
        """A watched file changed (delivered on the GUI thread)."""
        self._count_timer.start()
        self.browser_view.tree_view.viewport().update()

    @Slot(str)
    def _on_watcher_error(self, message: str):
        self._set_status(f"Live updates stopped: {message}")

    # ------------------------------------------------------------------ search
    def _set_status(self, text: str) -> None:
        self.status_lbl.setText(text)

    def _on_filters_changed(self):
        self._sync_sidebar()
        if self.search_view.search_input.text().strip():
            self.run_search()

    def run_search(self, raw_query: Optional[str] = None):
        if raw_query is None or not isinstance(raw_query, str):
            raw_query = self.search_view.search_input.text()
        raw_query = raw_query.strip()
        if not raw_query:
            self._retire_search_worker()
            self.search_view.model.clear()
            self.search_view.raw_results = []
            self._refresh_empty_state()
            return

        filters = self.filter_panel.get_filter_values()
        extended_query = raw_query
        if filters["extension"]:
            extended_query += f" ext:{filters['extension']}"
        if filters["size_range"]:
            lo, hi = filters["size_range"]
            if lo is not None and hi is not None:
                extended_query += f" size:{lo}..{hi}"
            elif lo is not None:
                extended_query += f" size:>{lo}"
            elif hi is not None:
                extended_query += f" size:<{hi}"

        self._set_status("Searching…")
        # Supersede a running search: never terminate() a thread that may be inside
        # SQLite; ignore its result and let it finish on its own.
        self._retire_search_worker()
        self.search_worker = SearchWorker(
            self.search_engine, raw_query, extended_query, filters,
            config_manager.config.max_results, list(config_manager.config.index_paths),
        )
        self.search_worker.results_ready.connect(self._on_search_completed)
        self.search_worker.error.connect(self._on_search_failed)
        self.search_worker.start()

    def _retire_search_worker(self):
        worker = self.search_worker
        self.search_worker = None
        if worker is None:
            return
        for sig in (worker.results_ready, worker.error):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        if worker.isRunning():
            worker.requestInterruption()
            self._stale_search_workers.append(worker)
            worker.finished.connect(lambda w=worker: self._forget_search_worker(w))

    def _forget_search_worker(self, worker):
        if worker in self._stale_search_workers:
            self._stale_search_workers.remove(worker)

    def _is_stale(self) -> bool:
        """A result queued by a superseded worker (or after shutdown) must be dropped:
        disconnecting does not cancel signals already posted to the event loop."""
        sender = self.sender()
        return self._shutdown_done or (sender is not None and sender is not self.search_worker)

    @Slot(object, str)
    def _on_search_completed(self, resp, raw_query: str):
        if self._is_stale():
            return
        try:
            terms = self.search_engine.parse_query(raw_query)["terms"]
        except Exception:
            terms = [raw_query]
        self.search_view.set_results(resp, terms)
        shown = self.preview_panel.current_path
        if shown and self.search_view.model.row_of(shown) < 0:
            self.preview_panel.clear()
        self.preview_panel.set_search_terms(terms)
        n = resp.total_count
        live = resp.query_time_ms < 0
        msg = f"{_plural(n, 'result')} in {_format_ms(abs(resp.query_time_ms))}"
        if live and n:
            msg += " (the index had no matches, so the folders were searched directly)"
        if n >= config_manager.config.max_results:
            msg += f". Showing the first {format_count(n)}; refine the search to see others"
        self._set_status(msg)
        if n > 0:
            self.search_history.add_search(raw_query, n)
        elif not getattr(resp, "error", None):
            self._show_no_matches(raw_query)

    def _show_no_matches(self, query: str) -> None:
        sv = self.search_view
        if sv.empty_state.kind != "no-matches":
            return
        title = f"No files match “{display_text(query)}”"
        actions = []
        if self.filter_panel.active_filter_count():
            message = "Some filters are on. Reset them or check the indexed folders."
            actions.append(("Reset filters", self.filter_panel.reset_filters))
        elif self.is_indexing():
            message = "Indexing is still running, so more files may match soon."
        else:
            message = "Check the spelling, the filters or the indexed folders."
        sv.show_empty("no-matches", title, message, actions)

    @Slot(str)
    def _on_search_failed(self, err_msg: str):
        if self._is_stale():
            return
        self._set_status("Search failed")
        self.search_view.model.clear()
        self.search_view.show_empty("error", "Search failed", err_msg)

    def _on_search_cleared(self):
        self._retire_search_worker()
        self.preview_panel.clear()
        self._set_status("Ready")
        self._refresh_empty_state()

    def _refresh_empty_state(self) -> None:
        sv = self.search_view
        if sv.search_input.text().strip() and sv.empty_state.kind not in ("ready", "indexing", "no-folders"):
            return
        if sv.search_input.text().strip() and sv.is_showing_results():
            return
        paths = config_manager.config.index_paths
        if not paths:
            home = get_default_index_root()
            sv.show_empty("no-folders", "No folders are indexed yet",
                          "Add a folder to search the files in it. The index stays on this computer.",
                          [("Add folder…", self.add_folder),
                           ("Index home folder", lambda: self.on_folder_selected(home))])
        elif self.is_indexing() and self._indexed_count == 0:
            name = self._folder_name(self._index_root or paths[0])
            sv.show_empty("indexing", "Indexing…",
                          f"{_plural(self._index_processed, 'item')} found in “{name}” so far. "
                          "You can search while this runs.")
        else:
            folders = _plural(len(paths), "folder")
            sv.show_empty("ready", "Search by name",
                          f"Type part of a file or folder name and press Enter. "
                          f"{_plural(self._indexed_count, 'item')} in {folders} indexed.\n"
                          "Narrow it down with ext:pdf or size:>10mb.")

    # ------------------------------------------------------------------ interaction
    def focus_search(self):
        if self.isHidden() or self.isMinimized():
            self.show_window()
        self.search_view.focus_search()

    def _hide_from_escape(self):
        if self.tray_icon is not None:
            self.hide()
        else:
            self.showMinimized()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_browser_path_changed(self, path: str):
        if os.path.isfile(path):
            self.preview_panel.load_file(path)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add folder to index")
        if folder:
            self.on_folder_selected(os.path.normpath(folder))

    def on_folder_selected(self, path: str):
        """Add ``path`` to the indexed folders (if new), browse it and index it."""
        if not path or not os.path.isdir(path):
            return
        paths = config_manager.config.index_paths
        if not any(paths_equal(path, p) for p in paths):
            paths.append(path)
            config_manager.save()
        self.browser_view.set_root_path(path)
        self.start_file_watchers()
        self.startIndexingFolder(path)

    def on_file_selected(self, path: str):
        self.preview_panel.load_file(path)
        self._update_item_actions()

    def on_folders_dropped(self, paths: list):
        cfg = config_manager.config
        for p in paths:
            if not any(paths_equal(p, q) for q in cfg.index_paths):
                cfg.index_paths.append(p)
        config_manager.save()
        self.start_file_watchers()
        self.startIndexing()

    def _focus_in_browser(self) -> bool:
        fw = QApplication.focusWidget()
        return fw is not None and self.browser_view.isAncestorOf(fw)

    def _update_item_actions(self) -> None:
        has = bool(self.search_view.selected_paths())
        one = len(self.search_view.selected_paths()) == 1
        for act in (self.act_open, self.act_copy_path, self.act_copy_name, self.act_trash):
            act.setEnabled(has)
        for act in (self.act_open_with, self.act_reveal):
            act.setEnabled(one)
        self.act_export.setEnabled(bool(self.search_view.raw_results))

    def _get_active_path(self) -> Optional[str]:
        return self.search_view.current_path()

    def copy_selected_path(self):
        if self._focus_in_browser():
            idx = self.browser_view.tree_view.currentIndex()
            if idx.isValid():
                self.search_view.copy_to_clipboard(self.browser_view.file_model.filePath(idx))
                self.statusBar().showMessage("Copied path", 4000)
            return
        self.search_view.copy_paths()

    def copy_selected_name(self):
        if not self._focus_in_browser():
            self.search_view.copy_names()

    def reveal_selected_path(self):
        if not self._focus_in_browser():
            self.search_view.reveal_selected()

    def open_selected_file(self):
        if not self._focus_in_browser():
            self.search_view.open_selected()

    def _open_with_selected(self):
        self.search_view.open_with_selected()

    def delete_selected_path(self):
        # Only results are deleted from here; never act while the folder tree has focus.
        if not self._focus_in_browser():
            self.search_view.trash_paths()

    def export_results(self):
        msg = export_results(self, [r.to_dict() for r in self.search_view.raw_results])
        if msg:
            self.statusBar().showMessage(msg, 6000)

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec():
            self.set_appearance(dialog.appearance)
            if dialog.indexing_changed:
                paths = config_manager.config.index_paths
                existing = [p for p in paths if os.path.isdir(p)]
                if existing:
                    self.browser_view.set_root_path(existing[0])
                self.start_file_watchers()
                self.startIndexing()
            self._update_index_count()
            self._refresh_empty_state()

    def open_about(self):
        AboutDialog(self).exec()

    # ------------------------------------------------------------------ tray & lifetime
    def setup_system_tray(self):
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
        menu.addAction(self.act_update_index)
        menu.addAction(self.act_settings)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray_icon = tray

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger and not theme.IS_MAC:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()
                self.search_view.focus_search()

    def close_and_quit(self):
        self._shutdown()
        QApplication.instance().quit()

    def _shutdown(self):
        """Save the window layout, stop background work and close the database. Idempotent."""
        if self._shutdown_done:
            return
        try:
            self._save_ui_state()
        except Exception:
            pass
        self._shutdown_done = True
        self._pending_index_jobs.clear()
        self._count_timer.stop()

        try:
            self.file_watcher.stop()
        except Exception:
            pass
        thread = self.indexer_thread
        if thread is not None and thread.isRunning():
            thread.stop()
            if not thread.wait(15000):
                thread.requestInterruption()
        self._retire_search_worker()
        for worker in list(self._stale_search_workers):
            worker.wait(5000)
        self.search_view.shutdown()
        self.preview_panel.shutdown()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        for closer in (self.search_history.close, self.db_index.close):
            try:
                closer()
            except Exception:
                pass

    def closeEvent(self, event: QCloseEvent):  # noqa: N802
        if (self.tray_icon is not None and not self._shutdown_done
                and theme.setting_bool("close_to_tray", False)):
            self._save_ui_state()
            self.hide()
            event.ignore()
            return
        self._shutdown()
        event.accept()
