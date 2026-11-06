"""Library sidebar (design F.3): indexed roots with live per-root counts, smart
filters and the Settings button.

The sidebar holds no filter state of its own: the window mirrors the Path chip into
``select_root`` and the Type/Modified/Favorites chips into ``set_smart_state``.
"""
from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QIcon, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileIconProvider, QFrame, QListView, QMenu, QSizePolicy,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem, QToolButton, QVBoxLayout, QWidget,
)

from . import theme
from .theme import SPACE_M, SPACE_S, SPACE_XS, display_text
from .widgets import SectionLabel, glyph_icon

ALL_ROOTS = -1   # row id of "All indexed folders"

SMART_FILTERS = (
    ("image", "Images", "image"),
    ("document", "Documents", "document"),
    ("code", "Code", "code"),
    ("recent", "Recently modified", "clock"),
    ("favorites", "Favorites", "star"),
)

RootIdRole = Qt.ItemDataRole.UserRole + 1
CountRole = Qt.ItemDataRole.UserRole + 2
StateRole = Qt.ItemDataRole.UserRole + 3
PathRole = Qt.ItemDataRole.UserRole + 4
CheckedRole = Qt.ItemDataRole.UserRole + 5
KeyRole = Qt.ItemDataRole.UserRole + 6

_STATE_TIPS = {
    "denied": ("macOS did not allow access. Open Privacy Settings…" if theme.IS_MAC
               else "Access to this folder was denied."),
    "missing": "Folder not found (unplugged or moved)",
    "error": "This folder could not be read.",
    "scanning": "Indexing…",
    "new": "Waiting to be indexed",
}


class LibraryModel(QAbstractListModel):
    """Rows: one per root, then "All indexed folders"."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.roots: List[object] = []                 # protocol.RootStatus
        self.counts: Dict[int, int] = {}
        self.all_count: Optional[int] = None
        self.mode = "entries"                          # 'entries' | 'results'
        self._folder_icon = QFileIconProvider().icon(QFileIconProvider.IconType.Folder)
        if self._folder_icon.isNull():
            self._folder_icon = glyph_icon("folder")

    def set_roots(self, roots: Sequence[object]) -> None:
        if [(r.id, r.name, r.path) for r in roots] == [(r.id, r.name, r.path) for r in self.roots]:
            self.roots = list(roots)
            self._changed()
            return
        self.beginResetModel()
        self.roots = list(roots)
        self.endResetModel()

    def set_counts(self, counts: Dict[int, int], all_count: Optional[int], mode: str) -> None:
        self.counts = dict(counts)
        self.all_count = all_count
        self.mode = mode
        self._changed()

    def _changed(self) -> None:
        if self.rowCount():
            self.dataChanged.emit(self.index(0), self.index(self.rowCount() - 1))

    def row_of(self, root_id: Optional[int]) -> int:
        if root_id is None:
            return len(self.roots)
        for i, r in enumerate(self.roots):
            if r.id == root_id:
                return i
        return -1

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.roots) + 1

    def _count(self, row: int) -> Optional[int]:
        if row == len(self.roots):
            if self.all_count is not None:
                return self.all_count
            return sum(r.entries for r in self.roots) if self.mode == "entries" else None
        r = self.roots[row]
        if self.mode == "entries":
            return r.entries
        return self.counts.get(r.id, 0)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        is_all = row == len(self.roots)
        r = None if is_all else self.roots[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return "All indexed folders" if is_all else display_text(r.name)
        if role == Qt.ItemDataRole.DecorationRole:
            if is_all:
                return None
            if r.state in ("denied", "error"):
                return glyph_icon("warning", "error")
            return self._folder_icon
        if role == RootIdRole:
            return None if is_all else r.id
        if role == PathRole:
            return None if is_all else r.path
        if role == StateRole:
            return "ok" if is_all else r.state
        if role == CountRole:
            return self._count(row)
        if role == Qt.ItemDataRole.ToolTipRole:
            if is_all:
                return "Search every indexed folder"
            tip = _STATE_TIPS.get(r.state, "")
            return f"{r.path}\n{tip}" if tip else r.path
        if role == Qt.ItemDataRole.AccessibleTextRole:
            name = "All indexed folders" if is_all else display_text(r.name)
            n = self._count(row)
            if n is None:
                return name
            unit = ("result" if n == 1 else "results") if self.mode == "results" else ("item" if n == 1 else "items")
            text = f"{name}, {theme.format_count(n)} {unit}"
            if r is not None and r.state in _STATE_TIPS:
                text += f", {_STATE_TIPS[r.state]}"
            return text
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        row = index.row()
        if row < len(self.roots) and self.roots[row].state == "missing":
            # Still selectable (to rescan it from the menu), drawn disabled.
            return Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        return f


class SmartFilterModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.checked: Dict[str, bool] = {k: False for k, _t, _i in SMART_FILTERS}

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(SMART_FILTERS)

    def set_checked(self, state: Dict[str, bool]) -> None:
        self.checked.update({k: bool(v) for k, v in state.items() if k in self.checked})
        self.dataChanged.emit(self.index(0), self.index(self.rowCount() - 1))

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        key, text, icon = SMART_FILTERS[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return text
        if role == Qt.ItemDataRole.DecorationRole:
            return glyph_icon(icon)
        if role == KeyRole:
            return key
        if role == CheckedRole:
            return self.checked[key]
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{text}, {'on' if self.checked[key] else 'off'}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return {"recent": "Modified in the past 7 days", "favorites": "Only favorites"}.get(key, f"Only {text.lower()}")
        return None


class SidebarDelegate(QStyledItemDelegate):
    """Native item background, icon, name and a right-aligned tabular count.
    Smart filter rows draw their checked state as the selection."""

    def __init__(self, view: QListView, counts: bool):
        super().__init__(view)
        self.view = view
        self.counts = counts
        self.spin_phase = 0.0

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(option.rect.width(), max(option.fontMetrics.height() + SPACE_M, 16 + SPACE_M))

    def paint(self, painter: QPainter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        checked = not self.counts and bool(index.data(CheckedRole))
        text = str(opt.text)
        icon = QIcon(opt.icon)   # copy: the option's member is reset below
        opt.text = ""
        opt.icon = QIcon()
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        if checked:
            # An "on" smart filter: a faint accent tint and a check mark, so the one
            # accent-filled row in the sidebar stays the selected folder.
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.match_fill_color(opt.palette))
            painter.drawRoundedRect(QRectF(opt.rect).adjusted(1, 1, -1, -1), 3, 3)
            painter.restore()

        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        state = index.data(StateRole) or "ok"
        disabled = state == "missing"
        pal = opt.palette
        group = QPalette.ColorGroup.Disabled if disabled else QPalette.ColorGroup.Normal
        fg = pal.color(group, QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        sec = fg if selected else theme.secondary_text_color(pal)

        rect = opt.rect.adjusted(SPACE_S, 0, -SPACE_S, 0)
        isz = 16
        icon_rect = QRect(rect.left(), rect.center().y() - isz // 2 + 1, isz, isz)
        mode = QIcon.Mode.Disabled if disabled else (QIcon.Mode.Selected if selected and not self.counts
                                                     else QIcon.Mode.Normal)
        has_icon = not icon.isNull()
        if has_icon:
            icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter, mode)
            rect.setLeft(rect.left() + isz + SPACE_S)
        elif self.counts:
            rect.setLeft(rect.left() + isz + SPACE_S)

        painter.save()
        right = rect.right()
        if checked:
            glyph_icon("check", "text").paint(painter, QRect(right - isz + 1, icon_rect.top(), isz, isz),
                                              Qt.AlignmentFlag.AlignCenter, QIcon.Mode.Normal)
            right -= isz + SPACE_XS
        if self.counts:
            n = index.data(CountRole)
            if state == "scanning":
                self._paint_spinner(painter, QRect(right - isz + 2, rect.center().y() - 6, 12, 12), sec)
                right -= isz + SPACE_XS
            if n is not None:
                font = theme.tabular_font(opt.font)
                if selected:
                    font.setWeight(QFont.Weight.Bold)
                painter.setFont(font)
                painter.setPen(sec)
                s = theme.format_count(n)
                w = painter.fontMetrics().horizontalAdvance(s)
                painter.drawText(QRect(right - w, rect.top(), w, rect.height()),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), s)
                right -= w + SPACE_S
        painter.setFont(opt.font)
        painter.setPen(fg)
        tr = QRect(rect.left(), rect.top(), max(0, right - rect.left()), rect.height())
        painter.drawText(tr, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         opt.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, tr.width()))
        painter.restore()

    def _paint_spinner(self, p: QPainter, r: QRect, color) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(color)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        start = int(-self.spin_phase * 16)
        p.drawArc(QRectF(r).adjusted(1, 1, -1, -1), start, 270 * 16)
        p.restore()


class _SidebarList(QListView):
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.setAccessibleName(name)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizeAdjustPolicy(QAbstractItemView.SizeAdjustPolicy.AdjustToContents)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.viewport().setAutoFillBackground(False)
        self.setAutoFillBackground(False)

    def sizeHint(self) -> QSize:  # noqa: N802
        n = self.model().rowCount() if self.model() else 0
        h = self.sizeHintForRow(0) * n if n else 0
        return QSize(200, h + 2 * self.frameWidth())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(80, min(self.sizeHint().height(), 3 * (self.sizeHintForRow(0) or 24)))


class LibrarySidebar(QFrame):
    root_selected = Signal(object)            # root id or None ("All indexed folders")
    smart_toggled = Signal(str, bool)         # key, on
    settings_requested = Signal()
    add_folder_requested = Signal()
    rescan_requested = Signal(int)
    reveal_requested = Signal(str)
    remove_requested = Signal(int, str)
    exclude_requested = Signal(int, str)
    open_privacy_requested = Signal()
    folders_dropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("LibrarySidebar")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAcceptDrops(True)
        self.setMinimumWidth(160)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        self.library_label = SectionLabel("Library")
        col.addWidget(self.library_label)
        self.model = LibraryModel(self)
        self.list = _SidebarList("Library")
        self.list.setModel(self.model)
        self.delegate = SidebarDelegate(self.list, counts=True)
        self.list.setItemDelegate(self.delegate)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.selectionModel().currentChanged.connect(self._on_current)
        self.list.clicked.connect(self._on_clicked)
        col.addWidget(self.list)

        col.addSpacing(SPACE_M)
        self.smart_label = SectionLabel("Smart filters")
        col.addWidget(self.smart_label)
        self.smart_model = SmartFilterModel(self)
        self.smart_list = _SidebarList("Smart filters")
        self.smart_list.setModel(self.smart_model)
        self.smart_list.setItemDelegate(SidebarDelegate(self.smart_list, counts=False))
        self.smart_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.smart_list.clicked.connect(self._toggle_smart)
        self.smart_list.activated.connect(self._toggle_smart)
        col.addWidget(self.smart_list)
        col.addStretch(1)

        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("SidebarSettings")
        icon = QIcon.fromTheme("preferences-system")
        self.settings_btn.setIcon(icon if not icon.isNull() else glyph_icon("gear"))
        self.settings_btn.setText(theme.settings_label())
        self.settings_btn.setAccessibleName(theme.settings_label())
        self.settings_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_btn.setAutoRaise(True)
        self.settings_btn.clicked.connect(self.settings_requested)
        col.addWidget(self.settings_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._silent = False
        self._spin = QTimer(self)
        self._spin.setInterval(80)
        self._spin.timeout.connect(self._advance_spinner)
        self.select_root(None)

    # ------------------------------------------------------------------ data
    def set_roots(self, roots: Sequence[object]) -> None:
        current = self.selected_root()
        self._silent = True
        try:
            self.model.set_roots(roots)
            self.select_root(current if self.model.row_of(current) >= 0 else None)
        finally:
            self._silent = False
        self.list.updateGeometry()
        scanning = any(r.state == "scanning" for r in roots)
        if scanning and not self._spin.isActive():
            self._spin.start()
        elif not scanning:
            self._spin.stop()

    def set_counts(self, counts: Dict[int, int], all_count: Optional[int], mode: str) -> None:
        self.model.set_counts(counts, all_count, mode)

    def set_smart_state(self, state: Dict[str, bool]) -> None:
        self.smart_model.set_checked(state)

    def selected_root(self) -> Optional[int]:
        idx = self.list.currentIndex()
        return idx.data(RootIdRole) if idx.isValid() else None

    def select_root(self, root_id: Optional[int]) -> None:
        """Mirror the Path chip; never emits ``root_selected``."""
        row = self.model.row_of(root_id)
        if row < 0:
            row = self.model.row_of(None)
        idx = self.model.index(row)
        if self.list.currentIndex() == idx and self.list.selectionModel().isSelected(idx):
            return
        old = self._silent
        self._silent = True
        try:
            self.list.setCurrentIndex(idx)
        finally:
            self._silent = old

    def root_paths(self) -> List[str]:
        return [r.path for r in self.model.roots]

    # ------------------------------------------------------------------ events
    def _on_current(self, current: QModelIndex, _prev=None) -> None:
        if self._silent or not current.isValid():
            return
        self.root_selected.emit(current.data(RootIdRole))

    def _on_clicked(self, index: QModelIndex) -> None:
        pass  # selection changes are handled through currentChanged

    def _toggle_smart(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        key = index.data(KeyRole)
        self.smart_toggled.emit(key, not bool(index.data(CheckedRole)))

    def _advance_spinner(self) -> None:
        self.delegate.spin_phase = (self.delegate.spin_phase + 30) % 360
        for row, r in enumerate(self.model.roots):
            if r.state == "scanning":
                self.list.update(self.model.index(row))

    def _context_menu(self, pos) -> None:
        index = self.list.indexAt(pos)
        menu = QMenu(self)
        rid = index.data(RootIdRole) if index.isValid() else None
        if rid is not None:
            path = index.data(PathRole)
            state = index.data(StateRole)
            menu.addAction("Rescan", lambda: self.rescan_requested.emit(rid))
            reveal = menu.addAction(theme.reveal_label(), lambda: self.reveal_requested.emit(path))
            reveal.setEnabled(state != "missing")
            if state == "denied" and theme.IS_MAC:
                menu.addAction("Open Privacy Settings…", self.open_privacy_requested.emit)
            menu.addSeparator()
            menu.addAction("Exclude a subfolder…", lambda: self.exclude_requested.emit(rid, path))
            menu.addAction("Remove from index…", lambda: self.remove_requested.emit(rid, path))
            menu.addSeparator()
        menu.addAction("Add folder…", self.add_folder_requested.emit)
        self._last_menu = menu
        menu.popup(self.list.viewport().mapToGlobal(pos))

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        folders = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                p = os.path.normpath(url.toLocalFile())
                if os.path.isdir(p):
                    folders.append(p)
        if folders:
            self.folders_dropped.emit(folders)
            event.acceptProposedAction()
