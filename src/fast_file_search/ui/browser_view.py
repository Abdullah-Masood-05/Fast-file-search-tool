from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import List, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QTreeView, QMenu, QMessageBox, QFrame, QHeaderView, QStyledItemDelegate,
    QFileDialog
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QUrl, QDir
from PyQt6.QtGui import QIcon, QAction, QGuiApplication, QFileSystemModel, QPainter, QColor, QDesktopServices

from ..config import config_manager


class IndexStatusDelegate(QStyledItemDelegate):
    """Custom delegate to draw colored dots representing indexing status for folders."""
    def __init__(self, parent, get_status_callback):
        super().__init__(parent)
        self.get_status_callback = get_status_callback

    def paint(self, painter: QPainter, option, index):
        # Paint default item first
        super().paint(painter, option, index)

        # Draw a status indicator dot in the first column next to directory items
        if index.column() == 0:
            status = self.get_status_callback(index)
            if status:
                color_map = {
                    "indexed": QColor("#4CAF50"),   # Vibrant Green
                    "partial": QColor("#FFC107"),   # Amber Yellow
                    "excluded": QColor("#F44336"),  # Crimson Red
                }
                color = color_map.get(status, QColor("#9E9E9E"))
                
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setBrush(color)
                painter.setPen(Qt.PenStyle.NoPen)
                
                # Right aligned inside the cell
                r = option.rect
                dot_size = 7
                painter.drawEllipse(
                    r.right() - 20, 
                    r.top() + (r.height() - dot_size) // 2, 
                    dot_size, 
                    dot_size
                )
                painter.restore()


class BreadcrumbBar(QWidget):
    """Path breadcrumb navigation bar with clickable segments, folder chooser, and recent dropdown."""
    path_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_path = ""
        self.is_dark = False
        
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.setSpacing(6)

        # Select Folder Button
        self.select_folder_btn = QPushButton("📁 Select Folder")
        self.select_folder_btn.setToolTip("Choose a folder from your disk to browse and index")
        self.select_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_folder_btn.clicked.connect(self._on_select_folder_clicked)
        self.layout.addWidget(self.select_folder_btn)

        # Recent items dropdown
        self.recent_combo = QComboBox()
        self.recent_combo.setFixedSize(30, 28)
        self.recent_combo.setToolTip("Recent folders history")
        self.recent_combo.addItem("🕒")
        self.recent_combo.activated.connect(self._on_recent_selected)
        self.layout.addWidget(self.recent_combo)

        # Container for breadcrumb buttons
        self.buttons_container = QWidget()
        self.buttons_layout = QHBoxLayout(self.buttons_container)
        self.buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.buttons_layout.setSpacing(2)
        self.layout.addWidget(self.buttons_container)

        self.layout.addStretch()

        # Copy current path button
        self.copy_btn = QPushButton("📋")
        self.copy_btn.setFixedSize(30, 28)
        self.copy_btn.setToolTip("Copy folder path to clipboard")
        self.copy_btn.clicked.connect(self.copy_current_path)
        self.layout.addWidget(self.copy_btn)

        self.apply_theme(False)

    def _on_select_folder_clicked(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Browse & Index")
        if folder:
            self.path_selected.emit(folder)

    def apply_theme(self, is_dark: bool):
        self.is_dark = is_dark
        if is_dark:
            self.setStyleSheet("""
                QWidget {
                    background-color: #1C1C1E;
                }
                QPushButton {
                    font-family: 'Segoe UI';
                    font-size: 11px;
                    background-color: #2C2C2E;
                    border: 1px solid #3A3A3C;
                    border-radius: 4px;
                    padding: 4px 8px;
                    color: #FFFFFF;
                }
                QPushButton:hover {
                    background-color: #3A3A3C;
                    border-color: #48484A;
                }
                QComboBox {
                    background-color: #2C2C2E;
                    border: 1px solid #3A3A3C;
                    border-radius: 4px;
                    color: #FFFFFF;
                    padding-left: 2px;
                }
                QLabel {
                    color: #8E8E93;
                }
            """)
        else:
            self.setStyleSheet("""
                QWidget {
                    background-color: #F8F9FA;
                }
                QPushButton {
                    font-family: 'Segoe UI';
                    font-size: 11px;
                    background-color: #FFFFFF;
                    border: 1px solid #E0E0E0;
                    border-radius: 4px;
                    padding: 4px 8px;
                    color: #333333;
                }
                QPushButton:hover {
                    background-color: #EAEAEA;
                    border-color: #CCCCCC;
                }
                QComboBox {
                    background-color: #FFFFFF;
                    border: 1px solid #E0E0E0;
                    border-radius: 4px;
                    color: #333333;
                    padding-left: 2px;
                }
                QLabel {
                    color: #999999;
                }
            """)
        self.update_breadcrumb_styles()

    def update_breadcrumb_styles(self):
        color = "#FFFFFF" if self.is_dark else "#333333"
        sep_color = "#8E8E93" if self.is_dark else "#999999"
        for i in range(self.buttons_layout.count()):
            widget = self.buttons_layout.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                widget.setStyleSheet(f"color: {color}; font-weight: bold; border: none; background: transparent;")
            elif isinstance(widget, QLabel):
                widget.setStyleSheet(f"color: {sep_color}; font-size: 13px;")

    def set_path(self, path: str):
        if not path:
            return
        self.current_path = path

        # Update combo box recent history list safely
        self.recent_combo.blockSignals(True)
        if path not in [self.recent_combo.itemText(i) for i in range(self.recent_combo.count())]:
            self.recent_combo.addItem(path)
        self.recent_combo.setCurrentIndex(0)
        self.recent_combo.blockSignals(False)

        # Clear old buttons
        for i in reversed(range(self.buttons_layout.count())):
            self.buttons_layout.itemAt(i).widget().setParent(None)

        # Rebuild breadcrumb buttons
        norm_path = os.path.normpath(path)
        parts = norm_path.split(os.sep)
        
        accum_path = ""
        is_windows = platform.system() == "Windows"
        for i, part in enumerate(parts):
            if not part:
                continue
            
            if i == 0 and is_windows and ":" in part:
                accum_path = part + os.sep
            elif not accum_path and not is_windows:
                accum_path = os.sep + part
            else:
                accum_path = os.path.join(accum_path, part)

            btn = QPushButton(part)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, p=accum_path: self.path_selected.emit(p))
            self.buttons_layout.addWidget(btn)


            if i < len(parts) - 1:
                sep = QLabel("›")
                self.buttons_layout.addWidget(sep)

        self.update_breadcrumb_styles()

    def _on_recent_selected(self, index):
        if index > 0:
            path = self.recent_combo.itemText(index)
            self.path_selected.emit(path)

    def copy_current_path(self):
        if self.current_path:
            clipboard = QGuiApplication.clipboard()
            clipboard.setText(self.current_path)


class BrowserView(QFrame):
    """QTreeView-based file explorer integration with breadcrumbs and index indicators."""
    path_changed = pyqtSignal(str)
    folders_dropped = pyqtSignal(list)
    reindex_folder = pyqtSignal(str)
    select_folder = pyqtSignal(str)  # Emitted when user explicitly picks a new root folder

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("BrowserView")

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(5)

        # 1. Breadcrumb Bar
        self.breadcrumb_bar = BreadcrumbBar()
        self.breadcrumb_bar.path_selected.connect(self._on_folder_explicitly_selected)
        self.layout.addWidget(self.breadcrumb_bar)

        # 2. File System Model & Tree View
        self.file_model = QFileSystemModel()
        self.file_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        
        self.tree_view = QTreeView()
        self.tree_view.setModel(self.file_model)
        self.tree_view.setAlternatingRowColors(True)
        self.tree_view.setDragEnabled(True)
        self.tree_view.setAcceptDrops(True)
        self.tree_view.setDropIndicatorShown(True)
        
        # Apply header formatting
        header = self.tree_view.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)

        # Connect delegate for status indicators
        self.delegate = IndexStatusDelegate(self.tree_view, self.get_folder_indexing_status)
        self.tree_view.setItemDelegate(self.delegate)

        # Signals for single click & double click navigation
        self.tree_view.clicked.connect(self._on_item_clicked)
        self.tree_view.doubleClicked.connect(self._on_item_double_clicked)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self.open_context_menu)

        self.layout.addWidget(self.tree_view)

        # Custom presets state
        self.current_preset = "Detailed"
        self.apply_theme(False)

    def apply_theme(self, is_dark: bool):
        self.breadcrumb_bar.apply_theme(is_dark)
        if is_dark:
            self.setStyleSheet("""
                QFrame#BrowserView {
                    background-color: #1C1C1E;
                    border-right: 1px solid #2C2C2E;
                }
                QTreeView {
                    border: none;
                    color: #FFFFFF;
                    background-color: #1C1C1E;
                    alternate-background-color: #18181A;
                }
                QTreeView::item {
                    color: #FFFFFF;
                }
                QTreeView::item:hover {
                    background-color: #2C2C2E;
                }
                QTreeView::item:selected {
                    background-color: #0A84FF;
                    color: #FFFFFF;
                }
                QHeaderView::section {
                    background-color: #252528;
                    color: #FFFFFF;
                    border: 1px solid #2C2C2E;
                    padding: 4px;
                    font-weight: bold;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame#BrowserView {
                    background-color: #FFFFFF;
                    border-right: 1px solid #E0E0E0;
                }
                QTreeView {
                    border: none;
                    color: #222222;
                    background-color: #FFFFFF;
                    alternate-background-color: #F9F9FB;
                }
                QTreeView::item {
                    color: #222222;
                }
                QTreeView::item:hover {
                    background-color: #F2F2F7;
                }
                QTreeView::item:selected {
                    background-color: #007AFF;
                    color: #FFFFFF;
                }
                QHeaderView::section {
                    background-color: #F2F2F7;
                    color: #222222;
                    border: 1px solid #E0E0E0;
                    padding: 4px;
                    font-weight: bold;
                }
            """)

    def set_root_path(self, path: str):
        if not path or not os.path.exists(path):
            return
        root_idx = self.file_model.setRootPath(path)
        self.tree_view.setRootIndex(root_idx)
        self.breadcrumb_bar.set_path(path)

    def navigate_to_path(self, path: str):
        self.set_root_path(path)
        self.path_changed.emit(path)

    def _on_folder_explicitly_selected(self, path: str):
        """Called when user explicitly picks a folder via 'Select Folder' button."""
        self.set_root_path(path)
        self.path_changed.emit(path)
        self.select_folder.emit(path)  # Tells MainWindow to add to index_paths and index

    def get_folder_indexing_status(self, index) -> Optional[str]:
        """Calculates color state for index mapping indicator."""
        path = self.file_model.filePath(index)
        if not path or not os.path.isdir(path):
            return None

        # Excluded check
        for pat in config_manager.config.exclude_patterns:
            if pat in path:
                return "excluded"

        # Fully indexed check
        for ip in config_manager.config.index_paths:
            if Path(path) == Path(ip) or Path(ip) in Path(path).parents:
                return "indexed"
            if Path(path) in Path(ip).parents:
                return "partial"

        return None

    def _on_item_clicked(self, index):
        path = self.file_model.filePath(index)
        self.path_changed.emit(path)

    def _on_item_double_clicked(self, index):
        path = self.file_model.filePath(index)
        if not path:
            return
        if os.path.isdir(path):
            # Double clicking a directory sets browser view root into that directory!
            self.set_root_path(path)
            self.path_changed.emit(path)
        elif os.path.isfile(path):
            # Double clicking a file opens it using OS default application
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # --- ACTIONS & CUSTOM COLUMN PRESETS ---

    def apply_column_preset(self, preset_name: str):
        """Show/hide columns dynamically based on presets."""
        self.current_preset = preset_name
        header = self.tree_view.header()
        
        if preset_name == "Minimal":
            header.setSectionHidden(1, True)
            header.setSectionHidden(2, True)
            header.setSectionHidden(3, True)
        elif preset_name == "Media":
            header.setSectionHidden(1, False)
            header.setSectionHidden(2, False)
            header.setSectionHidden(3, True)
        else:  # Detailed
            header.setSectionHidden(1, False)
            header.setSectionHidden(2, False)
            header.setSectionHidden(3, False)

    def open_context_menu(self, position):
        index = self.tree_view.indexAt(position)
        if not index.isValid():
            return
            
        path = self.file_model.filePath(index)
        is_dir = os.path.isdir(path)

        menu = QMenu(self)

        if is_dir:
            open_folder_act = QAction("Open & Navigate to Folder", self)
            open_folder_act.triggered.connect(lambda: self.set_root_path(path))
            menu.addAction(open_folder_act)

            idx_now_act = QAction("Index this folder now", self)
            idx_now_act.triggered.connect(lambda: self.reindex_folder.emit(path))
            menu.addAction(idx_now_act)

            exclude_act = QAction("Exclude folder from index", self)
            exclude_act.triggered.connect(lambda: self.exclude_folder(path))
            menu.addAction(exclude_act)

            menu.addSeparator()
            
            stats_act = QAction("Show folder stats...", self)
            stats_act.triggered.connect(lambda: self.show_folder_stats(path))
            menu.addAction(stats_act)
            
            menu.addSeparator()

        preset_menu = menu.addMenu("Column Preset")
        for preset in ["Detailed", "Minimal", "Media"]:
            act = QAction(preset, self)
            act.setCheckable(True)
            act.setChecked(self.current_preset == preset)
            act.triggered.connect(lambda checked, p=preset: self.apply_column_preset(p))
            preset_menu.addAction(act)

        menu.exec(self.tree_view.mapToGlobal(position))

    def exclude_folder(self, path: str):
        if path not in config_manager.config.exclude_patterns:
            config_manager.config.exclude_patterns.append(path)
            config_manager.save()
            self.tree_view.viewport().update()

    def show_folder_stats(self, path: str):
        """Walk path to count sizes/types and display properties."""
        p = Path(path)
        total_size = 0
        file_count = 0
        extensions = []
        
        try:
            for root, dirs, files in os.walk(path):
                for f in files:
                    file_count += 1
                    fp = os.path.join(root, f)
                    try:
                        total_size += os.path.getsize(fp)
                        extensions.append(Path(fp).suffix.lower())
                    except OSError:
                        pass
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read directory statistics: {e}")
            return

        from collections import Counter
        ext_count = Counter(extensions).most_common(5)
        ext_str = ", ".join([f"{ext} ({cnt})" for ext, cnt in ext_count])

        from ..utils.file_utils import format_size
        QMessageBox.information(
            self, "Folder Statistics",
            f"Path: {path}\n"
            f"Total Files: {file_count}\n"
            f"Total Size: {format_size(total_size)}\n"
            f"Top Extensions: {ext_str or 'None'}"
        )

    # --- DRAG AND DROP ---

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        folders = [u.toLocalFile() for u in urls if os.path.isdir(u.toLocalFile())]
        if folders:
            self.folders_dropped.emit(folders)
            event.acceptProposedAction()
