from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Any

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTreeWidget, QTreeWidgetItem, QMenu, QMessageBox, QListWidget,
    QInputDialog, QFileDialog, QHeaderView, QFrame
)
import threading
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QUrl, QEvent, QThread, QTimer, pyqtSlot
from PyQt6.QtGui import QIcon, QAction, QGuiApplication, QDesktopServices, QCursor, QFont

from .widgets import ExportDialog
from ..search_history import SearchHistory, Autocomplete, SearchRecommender
from ..search import SearchResult, SearchResponse
from ..utils.file_utils import format_size



class SearchResultItem(QTreeWidgetItem):
    """Custom QTreeWidgetItem that sorts columns numerically or chronologically."""
    def __init__(self, parent: QTreeWidget, result: SearchResult, history_mgr: SearchHistory):
        # Format date nicely
        import datetime
        try:
            date_str = datetime.datetime.fromtimestamp(result.modified).strftime('%Y-%m-%d %H:%M')
        except Exception:
            date_str = "--"

        # Star favorite status indicator
        is_fav = history_mgr.is_favorite(result.path)
        star_prefix = "⭐ " if is_fav else ""

        super().__init__(parent, [
            star_prefix + result.name,
            result.extension,
            format_size(result.size) if not result.is_folder else "Folder",
            date_str,
            result.path,
            f"{result.score:.2f}"
        ])
        self.result = result
        self.history_mgr = history_mgr

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        if not isinstance(other, SearchResultItem):
            return super().__lt__(other)
        
        column = self.treeWidget().sortColumn()
        if column == 2:  # Size column
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

    def __init__(self, parent: QWidget = None):
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

    def fetch(self, query: str):
        with self._mutex:
            self.query = query
        if not self.isRunning():
            self.start()

    def run(self):
        while True:
            with self._mutex:
                q = self.query
                self.query = ""
            if not q:
                break
            try:
                results = self.autocomplete.suggest(q, limit=6)
            except Exception:
                results = []
            self.suggestions_ready.emit(q, results)


class SearchView(QWidget):

    """Search operations panel: query entry, suggestions, and results tree view."""
    search_triggered = pyqtSignal(str)
    file_selected = pyqtSignal(str)  # Emits selected file path

    def __init__(self, parent=None, history_mgr: Optional[SearchHistory] = None):
        super().__init__(parent)
        self.history_mgr = history_mgr or SearchHistory()
        self.autocomplete = Autocomplete()
        self.recommender = SearchRecommender()

        # Layout
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(10)

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
        
        self.search_input.resizeEvent = lambda event: self.clear_btn.move(
            self.search_input.width() - 25, 
            (self.search_input.height() - self.clear_btn.height()) // 2
        )

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

        self.layout.addLayout(self.search_bar_layout)

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
        self.layout.addWidget(self.dym_lbl)

        # 2. Results Table View
        self.results_tree = QTreeWidget()
        self.results_tree.setColumnCount(6)
        self.results_tree.setHeaderLabels(["Name", "Extension", "Size", "Date Modified", "Path", "Score"])
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.setSortingEnabled(True)
        self.results_tree.setRootIsDecorated(False)
        self.results_tree.setFont(QFont("Segoe UI", 10))
        self.results_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        
        # Stretch sections
        header = self.results_tree.header()
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

        self.layout.addWidget(self.results_tree)

        self.raw_results: List[SearchResult] = []
        self.apply_theme(False)

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
        self.results_tree.clear()
        self.raw_results = response.results
        
        self.results_tree.setSortingEnabled(False)
        
        items = []
        for r in response.results:
            items.append(SearchResultItem(self.results_tree, r, self.history_mgr))
            
        self.results_tree.addTopLevelItems(items)
        self.results_tree.setSortingEnabled(True)
        self.results_tree.sortByColumn(5, Qt.SortOrder.DescendingOrder)

        if response.did_you_mean:
            self.dym_lbl.setText(f"Did you mean: <a href='{response.did_you_mean}' style='color: #007AFF;'>{response.did_you_mean}</a>?")
            self.dym_lbl.setVisible(True)
        else:
            self.dym_lbl.setVisible(False)

    def toggle_fav_filter(self):
        self.fav_filter_active = self.fav_btn.isChecked()
        self.filter_visible_items()

    def filter_visible_items(self):
        """Show only favorites if toggled."""
        for i in range(self.results_tree.topLevelItemCount()):
            item = self.results_tree.topLevelItem(i)
            if isinstance(item, SearchResultItem):
                if self.fav_filter_active:
                    is_fav = self.history_mgr.is_favorite(item.result.path)
                    item.setHidden(not is_fav)
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
        self.search_input.blockSignals(True)
        self.search_input.setText(text)
        self.search_input.blockSignals(False)
        self.search_triggered.emit(text)
        self.suggestions_popup.hide()

    def apply_dym_correction(self, text):
        self.search_input.setText(text)
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
                        if key == Qt.Key.Key_Down:
                            new_row = (cur_row + 1) % count
                        else:
                            new_row = (cur_row - 1 + count) % count
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
                    self.search_triggered.emit(self.search_input.text().strip())
                    self.suggestions_popup.hide()
                    return True
                    
            elif event.type() == QEvent.Type.FocusOut:
                QTimer.singleShot(250, self._check_focus_out)

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
        import platform
        import subprocess
        system = platform.system()
        if system == "Windows":
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        elif system == "Darwin":
            subprocess.call(["open", "-R", path])
        else:
            subprocess.call(["xdg-open", os.path.dirname(path)])

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
                if os.path.isdir(path):
                    import shutil
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                
                root = self.results_tree.invisibleRootItem()
                root.removeChild(item)
                
                QMessageBox.information(self, "Deleted", "File was deleted successfully.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete file: {e}")
