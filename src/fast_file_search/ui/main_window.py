from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QStatusBar, QProgressBar, QLabel, QSystemTrayIcon, QMenu,
    QMessageBox, QApplication, QLineEdit, QTextEdit, QPlainTextEdit
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QSize, QPoint
from PyQt6.QtGui import QIcon, QAction, QKeySequence, QShortcut, QCursor, QGuiApplication

from .widgets import CustomTitleBar, FilterPanel, PreviewPanel, SettingsDialog, AboutDialog, app_icon
from .search_view import SearchView, SearchResultItem
from .browser_view import BrowserView
from ..config import config_manager
from ..persistent_index import PersistentIndex
from ..search import SearchEngine
from ..search_history import SearchHistory
from ..file_watcher import FileWatcher
from ..indexer import IndexStats, Indexer
from ..config import AppConfig
from ..utils.file_utils import ExcludeMatcher


class IndexerThread(QThread):
    """QThread that brings the persistent index up to date in the background.

    By default it performs an *incremental* update (only new/changed/deleted
    entries are touched, the on-disk index from previous runs is reused). Pass
    ``force_rebuild=True`` to re-read every entry of the given roots.
    """
    progress = pyqtSignal(int, int)  # processed, total (total == 0: unknown)
    done = pyqtSignal(object)        # Emits IndexStats
    error = pyqtSignal(str)

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
            # This thread is about to end: release its SQLite read connection.
            try:
                self.persistent_index.release_thread_connection()
            except Exception:
                pass

    def stop(self):
        self.indexer.stop()


class SearchWorker(QThread):
    """QThread to perform search operations off the main GUI thread."""
    results_ready = pyqtSignal(object, str)  # SearchResponse, raw_query
    error = pyqtSignal(str)

    def __init__(self, search_engine, raw_query: str, extended_query: str, filters: dict, max_results: int, index_paths: list):
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
                self.error.emit(f"Invalid regular expression: {exc}")
                return
            excludes = ExcludeMatcher(self.filters["exclude_patterns"])

            # Persistent db search
            resp = self.search_engine.search_persistent(self.extended_query, page_size=self.max_results)

            if getattr(resp, "error", None):
                self.error.emit(str(resp.error))
                return

            # If DB returned nothing, try live filesystem fallback
            # (plain substring only, so skip it for regex/wildcard/operator queries)
            plain = not any(c in self.extended_query for c in "*?:\"") and not self.extended_query.lower().startswith("re:")
            if resp.total_count == 0 and self.index_paths and plain:
                resp = self._filesystem_fallback_search(self.raw_query, self.max_results)

            # Post filter for date & regex
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
            # This QThread is ending: don't leave its SQLite read connection open.
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
                                    path=full_path,
                                    name=name,
                                    extension=ext,
                                    size=st.st_size,
                                    modified=int(st.st_mtime),
                                    is_folder=is_dir,
                                    score=score,
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
            results=results[:max_results],
            total_count=len(results),
            page=1,
            page_size=max_results,
            total_pages=1,
            query_time_ms=-elapsed,
        )



class MainWindow(QMainWindow):
    """Main window shell managing panes, watchdogs, config, styles, and tray menus."""
    # Emitted from the file-watcher's worker threads; Qt delivers them on the GUI thread.
    watcher_event = pyqtSignal(str, str)
    watcher_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._shutdown_done = False

        # Frameless window configuration
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowMinMaxButtonsHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        
        # Handle resize border variables
        self.resize_margin = 8
        self.drag_start_pos = QPoint()
        self.drag_start_size = QSize()
        self.resizing_edge = None

        # Data & Core Integration
        self.db_index = PersistentIndex()
        self.search_history = SearchHistory()
        self.search_engine = SearchEngine(persistent_index=self.db_index)
        self.file_watcher = FileWatcher(persistent_index=self.db_index, config=config_manager.config)

        self.indexer_thread: Optional[IndexerThread] = None
        self._pending_index_jobs: List[tuple] = []
        self.search_worker: Optional[SearchWorker] = None
        self._stale_search_workers: List[SearchWorker] = []

        self.watcher_event.connect(self.on_file_watcher_change)
        self.watcher_error.connect(self._on_watcher_error)
        self.file_watcher.on_change(lambda t, p: self.watcher_event.emit(t, p))
        self.file_watcher.on_error(lambda exc: self.watcher_error.emit(str(exc)))
        app = QApplication.instance()
        if app is not None:
            # Also covers quitting via the tray menu, which bypasses closeEvent.
            app.aboutToQuit.connect(self._shutdown)


        # Custom UI assembly
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # 1. Custom Title Bar
        self.title_bar = CustomTitleBar(self, "Fast File Search Pro")
        self.title_bar.theme_toggled.connect(self.apply_theme)
        self.main_layout.addWidget(self.title_bar)

        # 2. Main QSplitter (Left / Right Split)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # --- LEFT PANEL: Explorer & Filters ---
        self.left_widget = QWidget()
        left_layout = QVBoxLayout(self.left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self.browser_view = BrowserView()
        self.browser_view.path_changed.connect(self.on_browser_path_changed)
        self.browser_view.folders_dropped.connect(self.on_folders_dropped)
        self.browser_view.reindex_folder.connect(self.startIndexingFolder)
        self.browser_view.select_folder.connect(self.on_folder_selected)
        left_layout.addWidget(self.browser_view)

        self.filter_panel = FilterPanel()
        self.filter_panel.filters_changed.connect(self.run_search)
        left_layout.addWidget(self.filter_panel)

        self.main_splitter.addWidget(self.left_widget)

        # --- RIGHT PANEL: Search results & Previewer ---
        self.right_splitter = QSplitter(Qt.Orientation.Horizontal)

        self.search_view = SearchView(history_mgr=self.search_history, persistent_index=self.db_index)
        self.search_view.search_triggered.connect(self.run_search)
        self.search_view.file_selected.connect(self.on_file_selected)
        self.right_splitter.addWidget(self.search_view)

        self.preview_panel = PreviewPanel()
        self.right_splitter.addWidget(self.preview_panel)

        self.main_splitter.addWidget(self.right_splitter)
        self.main_layout.addWidget(self.main_splitter)

        # 3. Status Bar
        self.status_bar = QStatusBar()
        self.status_bar.setFixedHeight(22)
        
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setStyleSheet("font-size: 10px; margin-left: 5px;")
        self.status_bar.addWidget(self.status_lbl)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet("QProgressBar { border: none; background: #E0E0E0; } QProgressBar::chunk { background: #007AFF; }")
        self.status_bar.addPermanentWidget(self.progress_bar)

        self.main_layout.addWidget(self.status_bar)

        # Keyboard shortcuts
        self.setup_shortcuts()

        # System tray icon
        self.setup_system_tray()

        # Apply settings / theme
        self.resize(1150, 780)
        self.apply_theme(config_manager.config.theme)
        self.apply_initial_configurations()

        # Start watchdog file monitors on paths
        self.start_file_watchers()

        # Set default extension filter items
        self.update_extensions_dropdown()

    # --- THEME MANAGEMENT ---

    def apply_theme(self, theme_name: str):
        """Applies dynamic dark/light mode stylesheets to widgets."""
        is_dark = theme_name == "dark"
        
        if is_dark:
            stylesheet = """
                QMainWindow, QWidget#central_widget {
                    background-color: #121214;
                }
                QSplitter::handle {
                    background-color: #252528;
                }
                QStatusBar {
                    background-color: #1C1C1E;
                    border-top: 1px solid #2C2C2E;
                    color: #8E8E93;
                }
                QLabel {
                    color: #FFFFFF;
                }
                QTreeWidget, QTreeView {
                    background-color: #1C1C1E;
                    border: 1px solid #2C2C2E;
                    color: #FFFFFF;
                }
                QHeaderView::section {
                    background-color: #252528;
                    color: #FFFFFF;
                    border: 1px solid #2C2C2E;
                }
                QLineEdit, QComboBox, QDateEdit, QTextEdit, QListWidget, QSpinBox {
                    background-color: #2C2C2E;
                    border: 1px solid #3A3A3C;
                    color: #FFFFFF;
                }
                QFrame#FilterPanel, QFrame#PreviewPanel {
                    background-color: #1C1C1E;
                    border-color: #2C2C2E;
                }
                QPushButton {
                    background-color: #2C2C2E;
                    border: 1px solid #3A3A3C;
                    color: #FFFFFF;
                }
                QPushButton:hover {
                    background-color: #3A3A3C;
                }
                QPushButton:checked {
                    background-color: #0A84FF;
                    border-color: #0A84FF;
                }
            """
        else:
            stylesheet = """
                QMainWindow, QWidget#central_widget {
                    background-color: #F2F2F7;
                }
                QSplitter::handle {
                    background-color: #E5E5EA;
                }
                QStatusBar {
                    background-color: #FFFFFF;
                    border-top: 1px solid #D1D1D6;
                    color: #8E8E93;
                }
                QLabel {
                    color: #000000;
                }
                QTreeWidget, QTreeView {
                    background-color: #FFFFFF;
                    border: 1px solid #D1D1D6;
                    color: #222222;
                }
                QHeaderView::section {
                    background-color: #F2F2F7;
                    color: #000000;
                    border: 1px solid #D1D1D6;
                }
                QLineEdit, QComboBox, QDateEdit, QTextEdit, QListWidget, QSpinBox {
                    background-color: #FFFFFF;
                    border: 1px solid #CCCCCC;
                    color: #333333;
                }
                QFrame#FilterPanel, QFrame#PreviewPanel {
                    background-color: #FFFFFF;
                    border-color: #E5E5EA;
                }
                QPushButton {
                    background-color: #EFEFEF;
                    border: 1px solid #CCCCCC;
                    color: #333333;
                }
                QPushButton:hover {
                    background-color: #DDDDDD;
                }
                QPushButton:checked {
                    background-color: #007AFF;
                    border-color: #0056B3;
                    color: white;
                }
            """
        
        self.setStyleSheet(stylesheet)
        self.title_bar.setStyleSheet(f"""
            CustomTitleBar {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {"#121214" if is_dark else "#1E1E24"}, stop:1 {"#1C1C1E" if is_dark else "#2D2D35"});
                border-bottom: 1px solid {"#2C2C2E" if is_dark else "#15151A"};
            }}
            QPushButton {{
                background: transparent;
                border: none;
                color: #DCDCDC;
                font-family: 'Segoe UI';
                font-size: 11px;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background: #3E3E45;
                color: white;
            }}
            QPushButton#close_btn:hover {{
                background: #E81123;
                color: white;
            }}
        """)

        # Propagate theme to all child panels
        self.browser_view.apply_theme(is_dark)
        self.filter_panel.apply_theme(is_dark)
        self.search_view.apply_theme(is_dark)
        self.preview_panel.apply_theme(is_dark)

        # Update status bar colour
        if is_dark:
            self.status_bar.setStyleSheet("background-color: #1C1C1E; border-top: 1px solid #2C2C2E; color: #8E8E93;")
            self.status_lbl.setStyleSheet("font-size: 10px; margin-left: 5px; color: #8E8E93;")
            self.progress_bar.setStyleSheet("QProgressBar { border: none; background: #2C2C2E; } QProgressBar::chunk { background: #0A84FF; }")
        else:
            self.status_bar.setStyleSheet("background-color: #FFFFFF; border-top: 1px solid #D1D1D6; color: #8E8E93;")
            self.status_lbl.setStyleSheet("font-size: 10px; margin-left: 5px; color: #555555;")
            self.progress_bar.setStyleSheet("QProgressBar { border: none; background: #E0E0E0; } QProgressBar::chunk { background: #007AFF; }")

        # Keep window theme config persistent
        config_manager.config.theme = theme_name
        config_manager.save()

    # --- MOUSE EVENTS FOR CUSTOM FRAMELESS RESIZING ---

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            w, h = self.width(), self.height()
            
            # Determine if mouse press is near the border to trigger resizing
            edge = None
            if pos.x() >= w - self.resize_margin and pos.y() >= h - self.resize_margin:
                edge = "bottom-right"
            elif pos.x() >= w - self.resize_margin:
                edge = "right"
            elif pos.y() >= h - self.resize_margin:
                edge = "bottom"
                
            if edge:
                self.resizing_edge = edge
                self.drag_start_pos = event.globalPosition().toPoint()
                self.drag_start_size = self.size()
                event.accept()

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        w, h = self.width(), self.height()
        
        # Update hover cursor shape
        if not self.resizing_edge:
            if pos.x() >= w - self.resize_margin and pos.y() >= h - self.resize_margin:
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif pos.x() >= w - self.resize_margin:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif pos.y() >= h - self.resize_margin:
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
        
        # Handle active resizing drag action
        if self.resizing_edge and event.buttons() == Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self.drag_start_pos
            new_w = self.drag_start_size.width()
            new_h = self.drag_start_size.height()
            
            if self.resizing_edge in ("right", "bottom-right"):
                new_w = max(500, new_w + delta.x())
            if self.resizing_edge in ("bottom", "bottom-right"):
                new_h = max(400, new_h + delta.y())
                
            self.resize(new_w, new_h)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.resizing_edge = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # --- CORE INDEXING CONTROL ---

    def apply_initial_configurations(self):
        paths = config_manager.config.index_paths
        if not paths:
            cwd = os.getcwd()
            config_manager.config.index_paths.append(cwd)
            config_manager.save()
            paths = config_manager.config.index_paths

        if paths:
            self.browser_view.set_root_path(paths[0])
            self.startIndexing()


    def startIndexing(self, force: bool = False):
        """Bring the index up to date. Normally incremental (reuses the index
        stored on disk); ``force=True`` re-reads every entry of every folder."""
        paths = list(config_manager.config.index_paths)
        if not paths:
            self.status_lbl.setText("No folders selected for indexing. Go to settings.")
            return
        force = force is True  # ignore stray values passed by Qt signals
        self._launch_indexer(paths, force, "Indexing folders (full rebuild)..." if force else "Indexing folders (updating saved index)...")

    def startIndexingFolder(self, folder_path: str):
        """Forces direct re-indexing of a single path."""
        self._launch_indexer([folder_path], True, f"Indexing folder {folder_path}...")

    def _launch_indexer(self, paths: List[str], force: bool, message: str):
        if self.indexer_thread is not None and self.indexer_thread.isRunning():
            job = (tuple(paths), force)
            if job not in self._pending_index_jobs:
                self._pending_index_jobs.append(job)
            self.status_lbl.setText("Indexing already in progress; queued next run...")
            return

        self.status_lbl.setText(message)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Pulse status indicator

        thread = IndexerThread(
            paths,
            self.db_index,
            config_manager.config.exclude_patterns,
            force_rebuild=force,
            index_content=config_manager.config.index_content,
        )
        thread.progress.connect(self.on_indexer_progress)
        thread.done.connect(self.on_indexer_finished)
        thread.error.connect(self.on_indexer_error)
        thread.finished.connect(self._on_indexer_thread_finished)  # QThread's own signal
        self.indexer_thread = thread
        thread.start()

    def _on_indexer_thread_finished(self):
        if self._shutdown_done or not self._pending_index_jobs:
            return
        paths, force = self._pending_index_jobs.pop(0)
        self._launch_indexer(list(paths), force, "Indexing folders (updating saved index)...")

    @pyqtSlot(int, int)
    def on_indexer_progress(self, processed, total):
        if total > 0:
            self.status_lbl.setText(f"Indexing... Processed {processed} of {total} items")
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(processed)
        else:  # first scan: the total isn't known yet
            self.status_lbl.setText(f"Indexing... {processed} items scanned")
            self.progress_bar.setRange(0, 0)

    @pyqtSlot(object)
    def on_indexer_finished(self, stats: Optional[IndexStats]):
        self.progress_bar.setVisible(False)
        if stats:
            self.status_lbl.setText(
                f"Index up to date: {stats.total_files} files, {stats.total_folders} folders "
                f"({stats.added} new, {stats.updated} changed, {stats.removed} removed) in {stats.duration:.2f}s"
            )
        else:
            self.status_lbl.setText("Indexing complete.")
        
        # Refresh extension frequency lists
        self.update_extensions_dropdown()
        self.browser_view.tree_view.viewport().update()

    @pyqtSlot(str)
    def on_indexer_error(self, err_msg):
        self.progress_bar.setVisible(False)
        self.status_lbl.setText(f"Indexing failed: {err_msg}")
        QMessageBox.warning(self, "Indexing Error", f"An indexer operation failed: {err_msg}")

    def update_extensions_dropdown(self):
        """Queries the indexer database to find top extensions and updates filters."""
        try:
            self.filter_panel.update_extensions(self.db_index.get_top_extensions(15))
        except Exception:
            pass

    # --- REAL TIME WATCHDOG INTEGRATION ---

    def start_file_watchers(self):
        try:
            self.file_watcher.stop()
            # Watch exactly the folders configured in settings (drops removed ones).
            self.file_watcher.set_paths(config_manager.config.index_paths)
            self.file_watcher.start()
        except Exception as e:
            self.status_lbl.setText(f"Watcher error: {e}")

    @pyqtSlot(str, str)
    def on_file_watcher_change(self, event_type: str, path: str):
        """Triggered (on the GUI thread) when filesystem changes are caught by Watchdog."""
        self.status_lbl.setText(f"Index watcher: File {event_type} - {Path(path.split(' -> ')[-1]).name}")
        # Redraw indicators
        self.browser_view.tree_view.viewport().update()

    @pyqtSlot(str)
    def _on_watcher_error(self, message: str):
        self.status_lbl.setText(f"Watcher error: {message}")

    # --- SEARCH EXECUTION FLOW ---

    def run_search(self, raw_query: Optional[str] = None):
        if raw_query is None:
            raw_query = self.search_view.search_input.text().strip()
            
        if not raw_query:
            self.search_view.results_tree.clear()
            self.status_lbl.setText("Ready")
            return

        # Fetch panel filters
        filters = self.filter_panel.get_filter_values()

        # Build modified search query appending custom advanced filters
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

        self.status_lbl.setText("Searching...")
        
        # Supersede a still-running search: never terminate() a thread that may be
        # inside SQLite; just ignore its result and let it finish on its own.
        self._retire_search_worker()

        # Run search asynchronously in background QThread so GUI never freezes
        self.search_worker = SearchWorker(
            self.search_engine,
            raw_query,
            extended_query,
            filters,
            config_manager.config.max_results,
            list(config_manager.config.index_paths)
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

    @pyqtSlot(object, str)
    def _on_search_completed(self, resp, raw_query: str):
        self.search_view.set_results(resp)
        source_label = " (live)" if resp.query_time_ms < 0 else ""
        query_time = abs(resp.query_time_ms)
        self.status_lbl.setText(f"Found {resp.total_count} files in {query_time:.1f}ms{source_label}")

        if resp.total_count > 0:
            self.search_history.add_search(raw_query, resp.total_count)

        terms = self.search_engine.parse_query(raw_query)["terms"]
        self.preview_panel.set_search_terms(terms)

    @pyqtSlot(str)
    def _on_search_failed(self, err_msg: str):
        self.status_lbl.setText(f"Search failed: {err_msg}")



    # --- WIDGET INTERACTION CALLBACKS ---

    def on_browser_path_changed(self, path: str):
        if os.path.isfile(path):
            self.preview_panel.load_file(path)
        else:
            self.status_lbl.setText(f"Current Path: {path}")

    def on_folder_selected(self, path: str):
        """Called when user explicitly selects a new folder via the browser Select Folder button."""
        if not path or not os.path.isdir(path):
            return
        if path not in config_manager.config.index_paths:
            config_manager.config.index_paths.append(path)
            config_manager.save()
        self.browser_view.set_root_path(path)
        self.start_file_watchers()
        self.startIndexingFolder(path)
        self.status_lbl.setText(f"Indexing: {path}...")

    def on_file_selected(self, path: str):
        self.preview_panel.load_file(path)

    def on_folders_dropped(self, paths: list[str]):
        """Drop handler to index new folders."""
        for p in paths:
            if p not in config_manager.config.index_paths:
                config_manager.config.index_paths.append(p)
        config_manager.save()
        self.start_file_watchers()
        self.startIndexing()

    # --- SYSTEM TRAY INTEGRATION ---

    def setup_system_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        # Using a fallback text/tool tip if app icon is missing
        self.tray_icon.setToolTip("Fast File Search Pro")
        
        # Set system icon
        icon = app_icon()
        if not icon.isNull():
            self.tray_icon.setIcon(icon)
            self.setWindowIcon(icon)
            self.title_bar.icon_label.setPixmap(icon.pixmap(20, 20))
        else:
            self.tray_icon.setIcon(QIcon.fromTheme("system-search"))


        # Tray Context Menu
        menu = QMenu()
        show_act = QAction("Show Application", self)
        show_act.triggered.connect(self.showNormal)
        menu.addAction(show_act)

        idx_act = QAction("Rebuild index", self)
        idx_act.triggered.connect(lambda _checked=False: self.startIndexing(force=True))
        menu.addAction(idx_act)

        settings_act = QAction("Settings...", self)
        settings_act.triggered.connect(self.open_settings)
        menu.addAction(settings_act)

        menu.addSeparator()
        
        exit_act = QAction("Exit", self)
        exit_act.triggered.connect(self.close_and_quit)
        menu.addAction(exit_act)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.show()

    def close_and_quit(self):
        self._shutdown()
        QApplication.instance().quit()

    def _shutdown(self):
        """Stop every background activity and close the database. Idempotent."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._pending_index_jobs.clear()

        # Stop producers first: watcher threads and the indexer write to the DB.
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
        try:
            self.tray_icon.hide()
        except Exception:
            pass
        try:
            self.search_history.close()
        except Exception:
            pass
        try:
            self.db_index.close()
        except Exception:
            pass

    def closeEvent(self, event):
        self._shutdown()
        event.accept()

    # --- SHORTCUTS & ACTIONS ---

    def setup_shortcuts(self):
        # Ctrl+F Focus Search
        QShortcut(QKeySequence("Ctrl+F"), self, self.search_view.search_input.setFocus)
        
        # Ctrl+Shift+F Toggle Filter Panel
        QShortcut(QKeySequence("Ctrl+Shift+F"), self, self.toggle_filter_panel)
        
        # Ctrl+C Copy Selected Path
        QShortcut(QKeySequence("Ctrl+C"), self, self.copy_selected_path)
        
        # Ctrl+E Reveal in Explorer
        QShortcut(QKeySequence("Ctrl+E"), self, self.reveal_selected_path)
        
        # Ctrl+O Open File
        QShortcut(QKeySequence("Ctrl+O"), self, self.open_selected_file)
        
        # Delete Selected item from index
        QShortcut(QKeySequence("Delete"), self, self.delete_selected_path)
        
        # F5 Force Refresh index
        QShortcut(QKeySequence("F5"), self, self.startIndexing)

    def toggle_filter_panel(self):
        self.filter_panel.setVisible(not self.filter_panel.isVisible())

    def _get_active_path(self) -> Optional[str]:
        # Tries to get path from selected items in search results or directory browser
        items = self.search_view.results_tree.selectedItems()
        if items:
            return items[0].text(4)
        
        idx = self.browser_view.tree_view.currentIndex()
        if idx.isValid():
            return self.browser_view.file_model.filePath(idx)
        return None

    def _text_input_focused(self) -> bool:
        return isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit, QPlainTextEdit))

    def copy_selected_path(self):
        # Leave normal text copy alone when a text field has focus.
        if self._text_input_focused():
            return
        path = self._get_active_path()
        if path:
            QGuiApplication.clipboard().setText(path)
            self.status_lbl.setText("Path copied to clipboard")

    def reveal_selected_path(self):
        path = self._get_active_path()
        if path:
            self.search_view.reveal_path(path)

    def open_selected_file(self):
        path = self._get_active_path()
        if path and os.path.exists(path):
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def delete_selected_path(self):
        # Only act when the results list itself has focus, never from a text field.
        if QApplication.focusWidget() is not self.search_view.results_tree:
            return
        items = self.search_view.results_tree.selectedItems()
        if items and isinstance(items[0], SearchResultItem):
            self.search_view.delete_file(items[0])

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec():
            # Apply changed settings
            self.start_file_watchers()
            self.startIndexing()

    def open_about(self):
        dialog = AboutDialog(self)
        dialog.exec()
