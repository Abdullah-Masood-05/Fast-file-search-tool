from __future__ import annotations

import os
from collections import Counter
from pathlib import PurePath
from typing import List, Optional, Tuple

from PyQt6.QtCore import QDir, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QFileSystemModel, QGuiApplication, QPainter, QPalette, QPen
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QMenu, QMessageBox, QProgressDialog,
    QSizePolicy, QStyle, QStyledItemDelegate, QToolButton, QToolTip, QTreeView, QVBoxLayout,
    QWidget,
)

from ..config import config_manager
from ..utils.platform_utils import is_path_under, path_key
from . import file_actions, theme
from .theme import SPACE_M, SPACE_S, SPACE_XS

_STATUS_TEXT = {
    "indexed": "Indexed",
    "partial": "Contains indexed folders",
    "excluded": "Excluded from the index",
}


class IndexStatusDelegate(QStyledItemDelegate):
    """Small marker at the right of folder rows: filled dot = indexed, ring = contains
    indexed folders, dash = excluded.  The tooltip says the same in words."""

    def __init__(self, parent, get_status_callback):
        super().__init__(parent)
        self.get_status_callback = get_status_callback

    def paint(self, painter: Optional[QPainter], option, index):
        super().paint(painter, option, index)
        if painter is None or index.column() != 0:
            return
        status = self.get_status_callback(index)
        if not status:
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        pal = option.palette
        if selected:
            color = pal.color(QPalette.ColorRole.HighlightedText)
        elif status == "excluded":
            color = theme.secondary_text_color(pal)
        else:
            color = pal.color(QPalette.ColorRole.Highlight)
        r = option.rect
        d = 6.0
        cx = r.right() - SPACE_M
        cy = r.center().y() + 0.5
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if status == "indexed":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(cx - d / 2, cy - d / 2, d, d))
        elif status == "partial":
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(cx - d / 2, cy - d / 2, d, d))
        else:
            painter.setPen(QPen(color, 1.5))
            painter.drawLine(QPointF(cx - d / 2, cy), QPointF(cx + d / 2, cy))
        painter.restore()

    def helpEvent(self, event, view, option, index):  # noqa: N802
        if event is not None and index.isValid() and index.column() == 0:
            status = self.get_status_callback(index)
            if status:
                name = index.data(Qt.ItemDataRole.DisplayRole) or ""
                QToolTip.showText(event.globalPos(), f"{name}\n{_STATUS_TEXT[status]}", view)
                return True
        return super().helpEvent(event, view, option, index)


def ancestor_targets(path: str) -> List[Tuple[str, str]]:
    """``[(label, path)]`` from the root down to ``path``.

    ``PurePath.parts`` understands drives ("C:\\"), UNC shares ("\\\\server\\share\\")
    and the POSIX root ("/") for the platform we run on.
    """
    parts = PurePath(os.path.normpath(path)).parts
    out = []
    for i, part in enumerate(parts):
        target = str(PurePath(*parts[: i + 1]))
        out.append((part.rstrip("\\/") or part, target))
    return out


class BreadcrumbBar(QWidget):
    """Location row: parent folder, current folder (menu with its ancestors and recent
    folders) and "Add folder…"."""
    path_selected = pyqtSignal(str)   # navigate only
    folder_chosen = pyqtSignal(str)   # explicit "Add folder…" pick (navigate and index)

    MAX_RECENT = 15

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_path = ""
        self.recent_paths: List[str] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)

        self.up_btn = QToolButton()
        self.up_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogToParent))
        self.up_btn.setAutoRaise(True)
        self.up_btn.setToolTip("Parent folder")
        self.up_btn.setAccessibleName("Parent folder")
        self.up_btn.clicked.connect(self.go_up)
        layout.addWidget(self.up_btn)

        self.path_btn = QToolButton()
        self.path_btn.setAutoRaise(True)
        self.path_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.path_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.path_btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.path_btn.setAccessibleName("Current folder")
        self.path_menu = QMenu(self)
        self.path_menu.aboutToShow.connect(self._fill_path_menu)
        self.path_btn.setMenu(self.path_menu)
        layout.addWidget(self.path_btn, 1)

        self.add_btn = QToolButton()
        self.add_btn.setText("Add folder…")
        self.add_btn.setToolTip("Choose a folder to browse and add to the index")
        self.add_btn.clicked.connect(self._on_select_folder_clicked)
        layout.addWidget(self.add_btn)

        self._update_controls()

    # -- actions
    def _on_select_folder_clicked(self):
        folder = QFileDialog.getExistingDirectory(self, "Add folder to index")
        if folder:
            # Qt hands back forward slashes on Windows; use the native form so the same
            # folder is never stored under two spellings.
            self.folder_chosen.emit(os.path.normpath(folder))

    def go_up(self):
        if not self.current_path:
            return
        parent = os.path.dirname(os.path.normpath(self.current_path))
        if parent and path_key(parent) != path_key(self.current_path):
            self.path_selected.emit(parent)

    def copy_current_path(self):
        if self.current_path:
            QGuiApplication.clipboard().setText(self.current_path)

    def _fill_path_menu(self):
        menu = self.path_menu
        menu.clear()
        if not self.current_path:
            return
        for label, target in reversed(ancestor_targets(self.current_path)):
            act = menu.addAction(theme.display_text(label))
            act.setData(target)
            act.triggered.connect(lambda _=False, p=target: self.path_selected.emit(p))
        others = [p for p in reversed(self.recent_paths) if path_key(p) != path_key(self.current_path)]
        if others:
            menu.addSection("Recent")
            for p in others:
                act = menu.addAction(theme.display_text(p))
                act.setData(p)
                act.triggered.connect(lambda _=False, p=p: self.path_selected.emit(p))
        menu.addSeparator()
        menu.addAction("Copy path", self.copy_current_path)

    # -- state
    def set_path(self, path: str):
        if not path:
            return
        self.current_path = path
        key = path_key(path)
        self.recent_paths = [p for p in self.recent_paths if path_key(p) != key] + [path]
        del self.recent_paths[:-self.MAX_RECENT]
        self._update_controls()

    def _update_controls(self):
        has = bool(self.current_path)
        self.up_btn.setEnabled(has and os.path.dirname(os.path.normpath(self.current_path))
                               not in ("", os.path.normpath(self.current_path)))
        self.path_btn.setEnabled(has)
        self.path_btn.setToolTip(theme.display_text(self.current_path))
        self._elide_path_text()

    def _elide_path_text(self):
        if not self.current_path:
            self.path_btn.setText("No folder")
            return
        crumbs = ancestor_targets(self.current_path)
        name = theme.display_text(crumbs[-1][0] if crumbs else self.current_path)
        # Leave room for the menu arrow and the button's own padding.
        width = max(0, self.path_btn.width() - 3 * SPACE_M)
        self.path_btn.setText(self.path_btn.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, width)
                              or name[:1])

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._elide_path_text()


class BrowserView(QFrame):
    """Folder tree of the current location, with index markers."""
    path_changed = pyqtSignal(str)
    folders_dropped = pyqtSignal(list)
    reindex_folder = pyqtSignal(str)
    select_folder = pyqtSignal(str)  # user explicitly picked a folder to add to the index

    PRESETS = (("Name only", "Minimal"), ("Name, size and type", "Media"), ("All columns", "Detailed"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("BrowserView")

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(SPACE_S)
        # Folders dragged in from the OS file manager are handled by dropEvent() below.
        self.setAcceptDrops(True)

        self.breadcrumb_bar = BreadcrumbBar()
        # Navigating never adds a folder to the index; only "Add folder…" does.
        self.breadcrumb_bar.path_selected.connect(self.navigate_to_path)
        self.breadcrumb_bar.folder_chosen.connect(self._on_folder_explicitly_selected)
        self.main_layout.addWidget(self.breadcrumb_bar)

        self.file_model = QFileSystemModel(self)
        self.file_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)

        self.tree_view = QTreeView()
        self.tree_view.setModel(self.file_model)
        self.tree_view.setAccessibleName("Folders")
        self.tree_view.setUniformRowHeights(True)
        self.tree_view.setDragEnabled(True)
        # The tree must not accept drops itself: the read-only model would reject them
        # and Qt would never offer the event to this widget.
        self.tree_view.setAcceptDrops(False)
        self.tree_view.setDropIndicatorShown(False)
        self.tree_view.setSortingEnabled(True)
        self.tree_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)

        header = self.tree_view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, header.ResizeMode.Stretch)

        self.delegate = IndexStatusDelegate(self.tree_view, self.get_folder_indexing_status)
        self.tree_view.setItemDelegate(self.delegate)

        self.tree_view.clicked.connect(self._on_item_clicked)
        self.tree_view.doubleClicked.connect(self._on_item_double_clicked)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self.open_context_menu)
        self.main_layout.addWidget(self.tree_view)

        self.current_preset = "Minimal"
        self.apply_column_preset(theme.ui_settings().value("browser/columns", "Minimal") or "Minimal")

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
        self.set_root_path(path)
        self.path_changed.emit(path)
        self.select_folder.emit(path)  # MainWindow adds it to index_paths and indexes it

    def get_folder_indexing_status(self, index) -> Optional[str]:
        """"indexed", "partial" (contains indexed folders), "excluded" or None."""
        if not self.file_model.isDir(index):  # cached by the model; no syscall per repaint
            return None
        path = self.file_model.filePath(index)
        if not path:
            return None
        key = path_key(path)
        for pat in config_manager.config.exclude_patterns:
            # blank patterns would otherwise match every path
            if pat and (pat in path or path_key(pat) in key):
                return "excluded"
        for ip in config_manager.config.index_paths:
            if not ip:
                continue
            if is_path_under(path, ip):
                return "indexed"
            if is_path_under(ip, path):
                return "partial"
        return None

    def _on_item_clicked(self, index):
        self.path_changed.emit(self.file_model.filePath(index))

    def _on_item_double_clicked(self, index):
        path = self.file_model.filePath(index)
        if not path:
            return
        if os.path.isdir(path):
            self.set_root_path(path)
            self.path_changed.emit(path)
        else:
            self._report(file_actions.open_path(path))

    def _report(self, error: Optional[str]) -> None:
        if error:
            QMessageBox.warning(self, theme.APP_NAME, error)

    # -- columns
    def apply_column_preset(self, preset_name: str):
        """Show/hide the size, type and date columns."""
        if preset_name not in ("Minimal", "Media", "Detailed"):
            preset_name = "Minimal"
        self.current_preset = preset_name
        header = self.tree_view.header()
        hidden = {"Minimal": (1, 2, 3), "Media": (3,), "Detailed": ()}[preset_name]
        for col in (1, 2, 3):
            header.setSectionHidden(col, col in hidden)
        self.tree_view.setHeaderHidden(preset_name == "Minimal")
        theme.ui_settings().setValue("browser/columns", preset_name)

    def open_context_menu(self, position):
        index = self.tree_view.indexAt(position)
        if not index.isValid():
            return
        path = self.file_model.filePath(index)
        is_dir = os.path.isdir(path)
        menu = QMenu(self)

        if is_dir:
            menu.addAction("Browse this folder", lambda: self.navigate_to_path(path))
        else:
            menu.addAction("Open", lambda: self._report(file_actions.open_path(path)))
        menu.addAction(theme.reveal_label(), lambda: self._report(file_actions.reveal(path)))
        menu.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(path))
        if is_dir:
            menu.addSeparator()
            menu.addAction("Update index for this folder", lambda: self.reindex_folder.emit(path))
            menu.addAction("Exclude from index", lambda: self.exclude_folder(path))
            menu.addAction("Folder statistics…", lambda: self.show_folder_stats(path))

        menu.addSeparator()
        columns = menu.addMenu("Columns")
        for label, preset in self.PRESETS:
            act = QAction(label, columns)
            act.setCheckable(True)
            act.setChecked(self.current_preset == preset)
            act.triggered.connect(lambda _=False, p=preset: self.apply_column_preset(p))
            columns.addAction(act)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def exclude_folder(self, path: str):
        if path not in config_manager.config.exclude_patterns:
            config_manager.config.exclude_patterns.append(path)
            config_manager.save()
            self.tree_view.viewport().update()

    def show_folder_stats(self, path: str):
        """Count files, total size and common types below ``path``."""
        total_size = 0
        file_count = 0
        ext_counter: Counter = Counter()
        cancelled = False

        # The walk runs on the GUI thread, so keep the event loop alive and let the user
        # cancel instead of freezing the window on huge trees.
        progress = QProgressDialog("Counting files…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Folder statistics")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)

        for root, dirs, files in os.walk(path, onerror=lambda e: None):
            for f in files:
                file_count += 1
                try:
                    total_size += os.path.getsize(os.path.join(root, f))
                    ext_counter[os.path.splitext(f)[1].lower()] += 1
                except OSError:
                    pass
                if file_count % 500 == 0:
                    progress.setLabelText(f"Counting files… {theme.format_count(file_count)}")
                    QApplication.processEvents()
                    if progress.wasCanceled():
                        cancelled = True
                        break
            if cancelled:
                break
        progress.cancel()

        top = ", ".join(f"{ext or 'no extension'} ({theme.format_count(cnt)})"
                        for ext, cnt in ext_counter.most_common(5))
        box = QMessageBox(self)
        box.setWindowTitle("Folder statistics")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(theme.display_text(os.path.basename(os.path.normpath(path)) or path))
        lines = [
            f"Files: {theme.format_count(file_count)}" + (" (counting stopped early)" if cancelled else ""),
            f"Total size: {theme.format_size(total_size)}",
            f"Most common types: {top or 'none'}",
        ]
        box.setInformativeText("\n".join(lines))
        box.exec()

    # --- drag and drop
    @staticmethod
    def _dropped_folders(event) -> List[str]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        return [p for p in paths if p and os.path.isdir(p)]

    def dragEnterEvent(self, event):  # noqa: N802
        if self._dropped_folders(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802
        if self._dropped_folders(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802
        folders = [os.path.normpath(p) for p in self._dropped_folders(event)]
        if folders:
            self.folders_dropped.emit(folders)
            event.acceptProposedAction()
        else:
            event.ignore()
