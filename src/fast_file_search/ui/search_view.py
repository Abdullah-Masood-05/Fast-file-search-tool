from __future__ import annotations

import html
import os
from pathlib import Path
from typing import List, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTreeWidget, QTreeWidgetItem, QMenu, QMessageBox, QListWidget,
    QInputDialog, QFileDialog, QHeaderView, QFrame, QApplication
)
import threading
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QUrl, QEvent, QThread, QTimer, pyqtSlot
from PyQt6.QtGui import QIcon, QAction, QGuiApplication, QDesktopServices, QCursor, QFont

from .widgets import ExportDialog
from ..persistent_index import PersistentIndex
from ..search_history import SearchHistory, Autocomplete, SearchRecommender
from ..search import SearchResult, SearchResponse
from ..utils.file_utils import format_size
from ..utils.platform_utils import open_file_manager




class SearchResultItem(QTreeWidgetItem):
    """Custom QTreeWidgetItem that sorts columns numerically or chronologically."""
    def __init__(
        self,
        parent: Optional[QTreeWidget],
        result: SearchResult,
        history_mgr: SearchHistory,
        is_favorite: Optional[bool] = None,
    ):
        # Format date nicely
        import datetime
        try:
            date_str = datetime.datetime.fromtimestamp(result.modified).strftime('%Y-%m-%d %H:%M')
        except Exception:
            date_str = "--"

        # Star favorite status indicator (callers can pass a precomputed value to avoid a
        # database round-trip per row).
        is_fav = history_mgr.is_favorite(result.path) if is_favorite is None else is_favorite
        star_prefix = "⭐ " if is_fav else ""

        columns = [
            star_prefix + result.name,
            result.extension,
            format_size(result.size) if not result.is_folder else "Folder",
            date_str,
            result.path,
            f"{result.score:.2f}"
        ]
        # Attaching to the tree here *and* via addTopLevelItems() later makes Qt re-index every
        # item (quadratic); bulk callers pass parent=None and add the items in one call.
        if parent is None:
            super().__init__(columns)
        else:
            super().__init__(parent, columns)
        self.result = result
        self.history_mgr = history_mgr

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        tree = self.treeWidget()
        if not isinstance(other, SearchResultItem) or tree is None:
            return super().__lt__(other)

        column = tree.sortColumn()
        if column == 0:  # Name: ignore the favorite star and letter case
            return self.result.name.lower() < other.result.name.lower()
        elif column == 2:  # Size column
            s1 = -1 if self.result.is_folder else self.result.size
            s2 = -1 if other.result.is_folder else other.result.size
            return s1 < s2
        elif column == 3:  # Modified Date
            return self.result.modified < other.result.modified
        elif column == 5:  # Score
            return self.result.score < other.result.score

        return super().__lt__(other)


class SuggestionsListPopup(QListWidget):
    """A floating list widget popup for query suggestions."""
    suggestion_selected = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # ToolTip stays on top without stealing focus; WA_ShowWithoutActivating
        # prevents the popup from ever grabbing window activation away from
        # the search input — this is the key fix for the "app loses focus" bug.
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.apply_theme(False)
        self.itemClicked.connect(self._on_item_clicked)


    def apply_theme(self, is_dark: bool):
        if is_dark:
            self.setStyleSheet("""
                QListWidget {
                    border: 1px solid #3A3A3C;
                    background-color: #2C2C2E;
                    color: #FFFFFF;
                    font-family: 'Segoe UI';
                    font-size: 11px;
                }
                QListWidget::item {
                    padding: 6px 10px;
                }
                QListWidget::item:hover, QListWidget::item:selected {
                    background-color: #0A84FF;
                    color: #FFFFFF;
                }
            """)
        else:
            self.setStyleSheet("""
                QListWidget {
                    border: 1px solid #CCCCCC;
                    background-color: #FFFFFF;
                    color: #333333;
                    font-family: 'Segoe UI';
                    font-size: 11px;
                }
                QListWidget::item {
                    padding: 6px 10px;
                }
                QListWidget::item:hover, QListWidget::item:selected {
                    background-color: #007AFF;
                    color: #FFFFFF;
                }
            """)

    def _on_item_clicked(self, item):
        self.suggestion_selected.emit(item.text())
        self.hide()


class SuggestionWorkerThread(QThread):

    """Background worker thread to fetch autocomplete suggestions without blocking the GUI thread."""
    suggestions_ready = pyqtSignal(str, list)  # query, suggestions

    def __init__(self, autocomplete: Autocomplete):
        super().__init__()
        self.autocomplete = autocomplete
        self.query = ""
        self._mutex = threading.Lock()
        self._active = False  # True while run() may still consume self.query
        self._stopping = False

    def fetch(self, query: str):
        with self._mutex:
            if self._stopping:
                return
            self.query = query
            need_start = not self._active
            if need_start:
                self._active = True
        if need_start:
            # The previous run() has already decided to exit (it cleared _active under the
            # mutex), so this only waits for the OS thread to wind down.
            self.wait()
            self.start()

    def run(self):
        while True:
            with self._mutex:
                q = self.query
                self.query = ""
                if not q:
                    # Decide to exit atomically with the emptiness check, otherwise a fetch()
                    # arriving in between would see "running" and its query would be lost.
                    self._active = False
                    self._release_connection()
                    return
            try:
                results = self.autocomplete.suggest(q, limit=6)
            except Exception:
                results = []
            self.suggestions_ready.emit(q, results)

    def _release_connection(self) -> None:
        """Close this worker thread's read-only DB connection once it goes idle."""
        release = getattr(getattr(self.autocomplete, "index", None), "release_thread_connection", None)
        if callable(release):
            try:
                release()
            except Exception:
                pass

    def shutdown(self):
        with self._mutex:
            self._stopping = True
            self.query = ""
        self.wait(3000)


class SearchView(QWidget):

    """Search operations panel: query entry, suggestions, and results tree view."""
    search_triggered = pyqtSignal(str)
    file_selected = pyqtSignal(str)  # Emits selected file path

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        history_mgr: Optional[SearchHistory] = None,
        persistent_index: Optional[PersistentIndex] = None,
    ):
        super().__init__(parent)
        self.history_mgr = history_mgr or SearchHistory()
        # Share the application's index when one is supplied (otherwise each helper opens
        # the default on-disk index on its own).
        self.index = persistent_index
        self.autocomplete = Autocomplete(persistent_index)
        self.recommender = SearchRecommender(persistent_index)

        # Layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(10)

        # 1. Search Bar & Suggestions layout
        self.search_bar_layout = QHBoxLayout()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by name, path, extension, or boolean filters (e.g. name:report AND size:>10mb)...")
        self.search_input.setFixedHeight(34)
        self.search_input.setFont(QFont("Segoe UI", 11))
        self.search_input.textChanged.connect(self._on_text_changed)
        self.search_input.installEventFilter(self)
        self.search_bar_layout.addWidget(self.search_input)

        # Clear button inside lineedit
        self.clear_btn = QPushButton("✕", self.search_input)
        self.clear_btn.setFixedSize(20, 20)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setStyleSheet("border: none; background: transparent; color: #999; font-size: 11px;")
        self.clear_btn.clicked.connect(self.clear_search)

        self._position_clear_button()

        # Actions Bar
        self.fav_filter_active = False
        self.fav_btn = QPushButton("⭐ Favorites")
        self.fav_btn.setCheckable(True)
        self.fav_btn.setFixedHeight(34)
        self.fav_btn.clicked.connect(self.toggle_fav_filter)
        self.search_bar_layout.addWidget(self.fav_btn)

        self.export_btn = QPushButton("📤 Export")
        self.export_btn.setFixedHeight(34)
        self.export_btn.clicked.connect(self.export_results)
        self.search_bar_layout.addWidget(self.export_btn)

        self.main_layout.addLayout(self.search_bar_layout)

        # Suggestions list popup
        self.suggestions_popup = SuggestionsListPopup()
        self.suggestions_popup.suggestion_selected.connect(self.apply_suggestion)

        # Debounce timer for lag-free typing (120ms)
        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(120)
        self.debounce_timer.timeout.connect(self._fetch_suggestions_debounced)

        # Worker thread for suggestions
        self.suggestion_thread = SuggestionWorkerThread(self.autocomplete)
        self.suggestion_thread.suggestions_ready.connect(self._on_suggestions_ready)


        # Typo correction notification ("Did you mean...")
        self.dym_lbl = QLabel()
        self.dym_lbl.setStyleSheet("color: #D32F2F; font-size: 11px; font-weight: bold; margin-left: 2px;")
        self.dym_lbl.setVisible(False)
        self.dym_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dym_lbl.linkActivated.connect(self.apply_dym_correction)
        self.main_layout.addWidget(self.dym_lbl)

        # 2. Results Table View
        self.results_tree = QTreeWidget()
        self.results_tree.setColumnCount(6)
        self.results_tree.setHeaderLabels(["Name", "Extension", "Size", "Date Modified", "Path", "Score"])
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.setSortingEnabled(True)
        self.results_tree.setRootIsDecorated(False)
        self.results_tree.setUniformRowHeights(True)  # lets Qt skip per-row size hints
        self.results_tree.setFont(QFont("Segoe UI", 10))
        self.results_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # Stretch sections
        header = self.results_tree.header()
        assert header is not None
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)

        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self.open_header_menu)

        # Signals
        self.results_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.results_tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.results_tree.customContextMenuRequested.connect(self.open_context_menu)

        self.main_layout.addWidget(self.results_tree)

        self.raw_results: List[SearchResult] = []
        self.apply_theme(False)

        # Never let Qt destroy the suggestion QThread while it is still running.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def shutdown(self) -> None:
        """Stop background workers; safe to call more than once."""
        self.debounce_timer.stop()
        self.suggestion_thread.shutdown()

    def _position_clear_button(self) -> None:
        self.clear_btn.move(
            self.search_input.width() - 25,
            (self.search_input.height() - self.clear_btn.height()) // 2,
        )

    def apply_theme(self, is_dark: bool):
        self.suggestions_popup.apply_theme(is_dark)
        if is_dark:
            self.setStyleSheet("""
                QWidget {
                    background-color: #121214;
                }
                QLineEdit {
                    border: 1px solid #3A3A3C;
                    border-radius: 6px;
                    padding-left: 10px;
                    padding-right: 30px;
                    background-color: #2C2C2E;
                    color: #FFFFFF;
                }
                QLineEdit:focus {
                    border-color: #0A84FF;
                }
                QPushButton {
                    font-family: 'Segoe UI';
                    font-size: 11px;
                    background-color: #2C2C2E;
                    color: #FFFFFF;
                    border: 1px solid #3A3A3C;
                    border-radius: 4px;
                    padding: 0 12px;
                }
                QPushButton:hover {
                    background-color: #3A3A3C;
                }
                QPushButton:checked {
                    background-color: #0A84FF;
                    color: white;
                    border-color: #0A84FF;
                }
                QTreeWidget {
                    border: 1px solid #2C2C2E;
                    border-radius: 6px;
                    color: #FFFFFF;
                    background-color: #1C1C1E;
                    alternate-background-color: #18181A;
                }
                QTreeWidget::item {
                    color: #FFFFFF;
                }
                QTreeWidget::item:hover {
                    background-color: #2C2C2E;
                }
                QTreeWidget::item:selected {
                    background-color: #0A84FF;
                    color: #FFFFFF;
                }
                QHeaderView::section {
                    background-color: #252528;
                    color: #FFFFFF;
                    padding: 5px;
                    font-weight: bold;
                    border: 1px solid #2C2C2E;
                }
            """)
        else:
            self.setStyleSheet("""
                QWidget {
                    background-color: #FFFFFF;
                }
                QLineEdit {
                    border: 1px solid #CCCCCC;
                    border-radius: 6px;
                    padding-left: 10px;
                    padding-right: 30px;
                    background-color: #FFFFFF;
                    color: #333333;
                }
                QLineEdit:focus {
                    border-color: #007AFF;
                }
                QPushButton {
                    font-family: 'Segoe UI';
                    font-size: 11px;
                    background-color: #EFEFEF;
                    color: #333333;
                    border: 1px solid #CCCCCC;
                    border-radius: 4px;
                    padding: 0 12px;
                }
                QPushButton:hover {
                    background-color: #DDDDDD;
                }
                QPushButton:checked {
                    background-color: #007AFF;
                    color: white;
                    border-color: #0056B3;
                }
                QTreeWidget {
                    border: 1px solid #CCCCCC;
                    border-radius: 6px;
                    color: #222222;
                    background-color: #FFFFFF;
                    alternate-background-color: #F9F9FB;
                }
                QTreeWidget::item {
                    color: #222222;
                }
                QTreeWidget::item:hover {
                    background-color: #F2F2F7;
                }
                QTreeWidget::item:selected {
                    background-color: #007AFF;
                    color: #FFFFFF;
                }
                QHeaderView::section {
                    background-color: #F8F9FA;
                    color: #222222;
                    padding: 5px;
                    font-weight: bold;
                    border: 1px solid #E0E0E0;
                }
            """)

    def clear_search(self):
        self.search_input.clear()
        self.results_tree.clear()
        self.dym_lbl.setVisible(False)
        self.suggestions_popup.hide()

    def set_results(self, response: SearchResponse):
        """Display search results inside tree widget."""
        tree = self.results_tree
        tree.setUpdatesEnabled(False)
        try:
            tree.clear()
            self.raw_results = list(response.results)

            tree.setSortingEnabled(False)

            # One query for all favourites instead of one per result row.
            favorites = set(self.history_mgr.get_favorites())
            items = [
                SearchResultItem(None, r, self.history_mgr, is_favorite=r.path in favorites)
                for r in response.results
            ]
            tree.addTopLevelItems(items)
            tree.setSortingEnabled(True)
            tree.sortByColumn(5, Qt.SortOrder.DescendingOrder)
            self.filter_visible_items()
        finally:
            tree.setUpdatesEnabled(True)

        error = getattr(response, "error", None)
        if error:
            self.dym_lbl.setTextFormat(Qt.TextFormat.PlainText)
            self.dym_lbl.setText(error)
            self.dym_lbl.setVisible(True)
        elif response.did_you_mean:
            # The suggestion is a file name: escape it before it goes into rich text / an href.
            safe = html.escape(response.did_you_mean, quote=True)
            self.dym_lbl.setTextFormat(Qt.TextFormat.RichText)
            self.dym_lbl.setText(f"Did you mean: <a href='{safe}' style='color: #007AFF;'>{safe}</a>?")
            self.dym_lbl.setVisible(True)
        else:
            self.dym_lbl.setVisible(False)

    def toggle_fav_filter(self):
        self.fav_filter_active = self.fav_btn.isChecked()
        self.filter_visible_items()

    def filter_visible_items(self):
        """Show only favorites if toggled."""
        favorites = set(self.history_mgr.get_favorites()) if self.fav_filter_active else set()
        for i in range(self.results_tree.topLevelItemCount()):
            item = self.results_tree.topLevelItem(i)
            if isinstance(item, SearchResultItem):
                if self.fav_filter_active:
                    item.setHidden(item.result.path not in favorites)
                else:
                    item.setHidden(False)

    def export_results(self):
        if not self.raw_results:
            QMessageBox.warning(self, "Export Results", "No results available to export.")
            return

        dialog = ExportDialog(self)
        dialog.set_results([r.to_dict() for r in self.raw_results])
        dialog.exec()

    def apply_suggestion(self, text):
        self.debounce_timer.stop()
        self.search_input.blockSignals(True)
        self.search_input.setText(text)
        self.search_input.blockSignals(False)
        self.search_triggered.emit(text)
        self.suggestions_popup.hide()

    def apply_dym_correction(self, text):
        self.debounce_timer.stop()
        self.search_input.blockSignals(True)
        self.search_input.setText(text)
        self.search_input.blockSignals(False)
        self.search_triggered.emit(text)

    def _on_text_changed(self, text):
        if not text.strip():
            self.suggestions_popup.hide()
            self.debounce_timer.stop()
            return
        # Restart 120ms debounce timer on every keystroke for 100% smooth typing
        self.debounce_timer.start()

    def _fetch_suggestions_debounced(self):
        text = self.search_input.text().strip()
        if text:
            self.suggestion_thread.fetch(text)
        else:
            self.suggestions_popup.hide()

    @pyqtSlot(str, list)
    def _on_suggestions_ready(self, query: str, suggestions: list):
        current_text = self.search_input.text().strip()
        if not current_text or current_text != query:
            return

        if suggestions:
            self.suggestions_popup.clear()
            self.suggestions_popup.addItems(suggestions)

            gp = self.search_input.mapToGlobal(QPoint(0, self.search_input.height()))
            popup_h = min(180, len(suggestions) * 28 + 10)
            self.suggestions_popup.setGeometry(gp.x(), gp.y(), self.search_input.width(), popup_h)
            self.suggestions_popup.setVisible(True)
            # Ensure the search input keeps keyboard focus — never let the popup steal it
            self.search_input.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.suggestions_popup.hide()


    def _check_focus_out(self):
        if not self.search_input.hasFocus() and not self.suggestions_popup.hasFocus():
            self.suggestions_popup.hide()

    def eventFilter(self, obj, event) -> bool:
        if obj == self.search_input:
            if event.type() == QEvent.Type.KeyPress:
                if self.suggestions_popup.isVisible():
                    key = event.key()
                    if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                        cur_row = self.suggestions_popup.currentRow()
                        count = self.suggestions_popup.count()
                        if count == 0:
                            return False
                        if key == Qt.Key.Key_Down:
                            new_row = (cur_row + 1) % count
                        elif cur_row < 0:
                            new_row = count - 1
                        else:
                            new_row = (cur_row - 1) % count
                        self.suggestions_popup.setCurrentRow(new_row)
                        return True
                    elif key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                        current_item = self.suggestions_popup.currentItem()
                        if current_item:
                            self.apply_suggestion(current_item.text())
                            return True
                    elif key == Qt.Key.Key_Escape:
                        self.suggestions_popup.hide()
                        return True

                if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                    # A pending debounce would pop the suggestions up again after the search.
                    self.debounce_timer.stop()
                    self.search_triggered.emit(self.search_input.text().strip())
                    self.suggestions_popup.hide()
                    return True

            elif event.type() == QEvent.Type.FocusOut:
                QTimer.singleShot(250, self._check_focus_out)
            elif event.type() == QEvent.Type.Resize:
                self._position_clear_button()

        return super().eventFilter(obj, event)


    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        if isinstance(item, SearchResultItem):
            path = item.result.path
            if os.path.exists(path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_selection_changed(self):
        selected = self.results_tree.selectedItems()
        if selected and isinstance(selected[0], SearchResultItem):
            self.file_selected.emit(selected[0].result.path)

    # --- RIGHT CLICK CONTEXT MENUS ---

    def open_header_menu(self, position):
        header = self.results_tree.header()
        assert header is not None
        menu = QMenu(self)

        for i in range(self.results_tree.columnCount()):
            col_name = self.results_tree.headerItem().text(i)
            action = QAction(col_name, self)
            action.setCheckable(True)
            action.setChecked(not header.isSectionHidden(i))
            action.triggered.connect(lambda checked, idx=i: header.setSectionHidden(idx, not checked))
            menu.addAction(action)

        menu.exec(header.mapToGlobal(position))

    def open_context_menu(self, position):
        item = self.results_tree.itemAt(position)
        if not item or not isinstance(item, SearchResultItem):
            return

        path = item.result.path
        is_starred = self.history_mgr.is_favorite(path)

        menu = QMenu(self)

        open_act = QAction("Open File", self)
        open_act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        menu.addAction(open_act)

        reveal_act = QAction("Reveal in containing folder", self)
        reveal_act.triggered.connect(lambda: self.reveal_path(path))
        menu.addAction(reveal_act)

        menu.addSeparator()

        copy_path_act = QAction("Copy Path", self)
        copy_path_act.triggered.connect(lambda: self.copy_to_clipboard(path))
        menu.addAction(copy_path_act)

        copy_name_act = QAction("Copy Filename", self)
        copy_name_act.triggered.connect(lambda: self.copy_to_clipboard(Path(path).name))
        menu.addAction(copy_name_act)

        menu.addSeparator()

        star_text = "Remove from Favorites" if is_starred else "Add to Favorites"
        star_act = QAction(star_text, self)
        star_act.triggered.connect(lambda: self.toggle_favorite(item))
        menu.addAction(star_act)

        tag_act = QAction("Manage Tags...", self)
        tag_act.triggered.connect(lambda: self.manage_tags(path))
        menu.addAction(tag_act)

        note_act = QAction("Add / Edit Note...", self)
        note_act.triggered.connect(lambda: self.manage_note(path))
        menu.addAction(note_act)

        menu.addSeparator()

        delete_act = QAction("Delete file from disk", self)
        delete_act.triggered.connect(lambda: self.delete_file(item))
        menu.addAction(delete_act)

        menu.exec(self.results_tree.mapToGlobal(position))

    def copy_to_clipboard(self, text):
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(text)

    def reveal_path(self, path):
        open_file_manager(path)


    def toggle_favorite(self, item: SearchResultItem):
        path = item.result.path
        if self.history_mgr.is_favorite(path):
            self.history_mgr.remove_favorite(path)
            item.setText(0, item.result.name)
        else:
            self.history_mgr.add_favorite(path)
            item.setText(0, "⭐ " + item.result.name)

        self.filter_visible_items()

    def manage_tags(self, path):
        existing_tags = ", ".join(self.history_mgr.get_tags(path))
        text, ok = QInputDialog.getText(self, "Manage Tags", "Enter tags (comma separated):", QLineEdit.EchoMode.Normal, existing_tags)
        if ok:
            for t in self.history_mgr.get_tags(path):
                self.history_mgr.remove_tag(path, t)
            new_tags = [t.strip() for t in text.split(",") if t.strip()]
            for nt in new_tags:
                self.history_mgr.add_tag(path, nt)

    def manage_note(self, path):
        existing_note = self.history_mgr.get_note(path)
        text, ok = QInputDialog.getMultiLineText(self, "Edit Annotation/Note", "Enter description/note for file:", existing_note)
        if ok:
            self.history_mgr.set_note(path, text.strip())

    def _forget_in_index(self, path: str) -> None:
        """Drop a deleted path from the search index so it does not reappear in results."""
        index = self.index or getattr(self.autocomplete, "index", None)
        if index is None:
            return
        try:
            index.remove_file(path)
        except Exception:
            pass

    def delete_file(self, item: SearchResultItem):
        path = item.result.path
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to permanently delete this file from disk?\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    import shutil
                    shutil.rmtree(path)
                else:
                    os.remove(path)

                root = self.results_tree.invisibleRootItem()
                if root is not None:
                    root.removeChild(item)
                self.raw_results = [r for r in self.raw_results if r.path != path]
                self._forget_in_index(path)

                QMessageBox.information(self, "Deleted", "File was deleted successfully.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete file: {e}")
