"""Results header, model, delegate and view (design F.4, F.5).

The model only shows what the engine sends: a first page (``set_result``), more pages
(``append_page``, driven by ``fetchMore``) and live refreshes (``apply_refresh``,
which keep the selection and scroll position). Sorting is a new engine request.
"""
from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import (
    QAbstractItemModel, QAbstractTableModel, QEvent, QFileInfo, QMimeData, QModelIndex, QPersistentModelIndex,
    QRect, QRectF, QSize, Qt, QUrl, Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QFont, QFontMetrics, QIcon, QKeyEvent, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileIconProvider, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QToolButton, QTreeView,
    QVBoxLayout, QWidget,
)

from ..engine.protocol import FOLDER, PACKAGE, ResultRow, SearchResult, SortSpec
from . import theme
from .theme import SPACE_M, SPACE_S, SPACE_XS, display_text
from .. import kinds
from .widgets import DropDownButton, EmptyState, glyph_icon

NAME, FOLDER_COL, SIZE, MODIFIED, KIND = range(5)
HEADERS = ("Name", "Folder", "Size", "Modified", "Kind")
COLUMN_SORT = {NAME: "name", FOLDER_COL: "folder", SIZE: "size", MODIFIED: "mtime", KIND: "kind"}
SORT_LABELS = {"relevance": "Relevance", "name": "Name", "folder": "Folder", "size": "Size",
               "mtime": "Modified", "kind": "Kind"}
PAGE_SIZE = 500

RowRole = Qt.ItemDataRole.UserRole.value + 1
PathRole = Qt.ItemDataRole.UserRole.value + 2

# Roles arrive as plain ints; comparing them with enum members goes through Python's
# enum machinery for every cell, so the hot paths use int values resolved once.
_DISPLAY = Qt.ItemDataRole.DisplayRole.value
_DECORATION = Qt.ItemDataRole.DecorationRole.value
_TOOLTIP = Qt.ItemDataRole.ToolTipRole.value
_ALIGNMENT = Qt.ItemDataRole.TextAlignmentRole.value
_FONT = Qt.ItemDataRole.FontRole.value
_FOREGROUND = Qt.ItemDataRole.ForegroundRole.value
_ACCESSIBLE = Qt.ItemDataRole.AccessibleTextRole.value
_UNUSED_ROLES = frozenset({Qt.ItemDataRole.BackgroundRole.value, Qt.ItemDataRole.CheckStateRole.value,
                           Qt.ItemDataRole.SizeHintRole.value, Qt.ItemDataRole.StatusTipRole.value,
                           Qt.ItemDataRole.WhatsThisRole.value})
_SELECTED = QStyle.StateFlag.State_Selected
_ACTIVE = QStyle.StateFlag.State_Active
_CE_ITEM = QStyle.ControlElement.CE_ItemViewItem
_SE_TEXT = QStyle.SubElement.SE_ItemViewItemText
_PM_FOCUS_H = QStyle.PixelMetric.PM_FocusFrameHMargin
_G_NORMAL = QPalette.ColorGroup.Normal
_G_INACTIVE = QPalette.ColorGroup.Inactive
_R_TEXT = QPalette.ColorRole.Text
_R_HTEXT = QPalette.ColorRole.HighlightedText
_ALIGN_RIGHT = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
_ALIGN_LEFT = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
_NO_FLAGS = Qt.ItemFlag.NoItemFlags
_ITEM_FLAGS = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
_LAYOUT_ARGS = ('QList<QPersistentModelIndex>', 'QAbstractItemModel::LayoutChangeHint')
_NATIVE_SEP = os.sep


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot > 0 else ""


class IconCache:
    """File-type icons cached per kind and extension. The platform is asked about a
    made-up file name (never a real path), so nothing on disk is touched."""

    _PROBE_DIR = os.path.join(os.path.abspath(os.sep), "__ffs_icon_probe__")

    def __init__(self):
        self.provider = QFileIconProvider()
        self._cache: Dict[str, QIcon] = {}

    def for_row(self, name: str, flags: int) -> QIcon:
        if flags & PACKAGE:
            key = "<package>"
        elif flags & FOLDER:
            key = "<folder>"
        else:
            key = _ext(name) or "<none>"
        icon = self._cache.get(key)
        if icon is None:
            icon = self._make(key)
            self._cache[key] = icon
        return icon

    def for_name(self, name: str) -> QIcon:
        return self.for_row(name, 0)

    def _make(self, key: str) -> QIcon:
        if key == "<folder>":
            icon = self.provider.icon(QFileIconProvider.IconType.Folder)
            return icon if not icon.isNull() else glyph_icon("folder")
        if key == "<package>":
            icon = self.provider.icon(QFileIconProvider.IconType.File)
            return icon if not icon.isNull() else glyph_icon("file")
        icon = QIcon()
        if key != "<none>":
            icon = self.provider.icon(QFileInfo(os.path.join(self._PROBE_DIR, "x." + key)))
        if icon.isNull():
            kind = kinds.kind_of("x." + key, 0) if key != "<none>" else "file"
            icon = glyph_icon({"image": "image", "document": "document", "code": "code"}.get(kind, "file"))
        return icon


_icon_cache: Optional[IconCache] = None


def icon_cache() -> IconCache:
    global _icon_cache
    if _icon_cache is None:
        _icon_cache = IconCache()
    return _icon_cache


# --------------------------------------------------------------------------- model
class ResultsModel(QAbstractTableModel):
    fetch_requested = Signal(int, int)       # gen, offset

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: List[ResultRow] = []
        self.total = 0
        self.gen = -1
        self._disp: List[Optional[tuple]] = []
        self._in_flight = False
        self.root_names: Dict[int, str] = {}
        self.root_paths: Dict[int, str] = {}
        self.prefix_root = False
        self._tabular = theme.tabular_font()
        self._icons = icon_cache()
        self._secondary = theme.secondary_text_color()

    # -- content
    def set_roots(self, roots: Iterable[object], prefix_root: bool) -> None:
        names = {r.id: r.name for r in roots}
        paths = {r.id: r.path for r in roots}
        if names != self.root_names or prefix_root != self.prefix_root or paths != self.root_paths:
            self.root_names, self.root_paths, self.prefix_root = names, paths, prefix_root
            self._disp = [None] * len(self.rows)
            if self.rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(HEADERS) - 1))

    def palette_changed(self) -> None:
        self._secondary = theme.secondary_text_color()
        self._tabular = theme.tabular_font()

    def set_result(self, res: SearchResult) -> None:
        self.beginResetModel()
        self.rows = list(res.rows)
        self._disp = [None] * len(self.rows)
        self.total = max(res.total, len(self.rows))
        self.gen = res.gen
        self._in_flight = False
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self.rows, self._disp, self.total, self._in_flight = [], [], 0, False
        self.endResetModel()

    def apply_refresh(self, res: SearchResult) -> None:
        """Replace the first page in place; persistent indexes (selection, current row,
        scroll anchor) follow their entry id."""
        hint = QAbstractItemModel.LayoutChangeHint.NoLayoutChangeHint
        self.layoutAboutToBeChanged[_LAYOUT_ARGS].emit([], hint)
        old = self.persistentIndexList()
        ids = [self.rows[i.row()].id if i.isValid() and i.row() < len(self.rows) else None for i in old]
        new_rows = list(res.rows)
        pos = {r.id: n for n, r in enumerate(new_rows)}
        self.rows = new_rows
        self._disp = [None] * len(new_rows)
        self.total = max(res.total, len(new_rows))
        self.gen = res.gen
        self._in_flight = False
        new = [self.index(pos[i], p.column()) if i is not None and i in pos else QModelIndex()
               for p, i in zip(old, ids)]
        self.changePersistentIndexList(old, new)
        self.layoutChanged[_LAYOUT_ARGS].emit([], hint)

    def append_page(self, res: SearchResult) -> bool:
        if res.gen != self.gen:
            return False
        self._in_flight = False
        if res.offset != len(self.rows) or not res.rows:
            self.total = max(len(self.rows), min(self.total, res.total))
            return False
        first = len(self.rows)
        self.beginInsertRows(QModelIndex(), first, first + len(res.rows) - 1)
        self.rows.extend(res.rows)
        self._disp.extend([None] * len(res.rows))
        self.total = max(res.total, len(self.rows))
        self.endInsertRows()
        return True

    def remove_paths(self, paths: Iterable[str]) -> None:
        gone = set(paths)
        for row in range(len(self.rows) - 1, -1, -1):
            if self.rows[row].path in gone:
                self.beginRemoveRows(QModelIndex(), row, row)
                del self.rows[row]
                del self._disp[row]
                self.total = max(0, self.total - 1)
                self.endRemoveRows()

    def set_favorite(self, path: str, on: bool) -> None:
        for row, r in enumerate(self.rows):
            if r.path == path and r.favorite != on:
                self.rows[row] = r._replace(favorite=on)
                self._disp[row] = None
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(HEADERS) - 1))

    def row(self, i: int) -> Optional[ResultRow]:
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def row_of_id(self, entry_id: int) -> int:
        for i, r in enumerate(self.rows):
            if r.id == entry_id:
                return i
        return -1

    def row_of_path(self, path: str) -> int:
        for i, r in enumerate(self.rows):
            if r.path == path:
                return i
        return -1

    # -- paging
    def canFetchMore(self, parent=QModelIndex()) -> bool:  # noqa: N802
        return not parent.isValid() and not self._in_flight and len(self.rows) < self.total and self.gen >= 0

    def fetchMore(self, parent=QModelIndex()) -> None:  # noqa: N802
        if not self.canFetchMore(parent):
            return
        self._in_flight = True
        self.fetch_requested.emit(self.gen, len(self.rows))

    @property
    def in_flight(self) -> bool:
        return self._in_flight

    # -- display strings (lazy, cached per row)
    def folder_text(self, r: ResultRow) -> str:
        root = self.root_names.get(r.root_id, "")
        rel = r.rel_dir.replace("/", _NATIVE_SEP) if _NATIVE_SEP != "/" else r.rel_dir
        if not rel:
            return display_text(root)
        if self.prefix_root and root:
            return display_text(root + _NATIVE_SEP + rel)
        return display_text(rel)

    def _display(self, row: int) -> tuple:
        d = self._disp[row]
        if d is None:
            r = self.rows[row]
            folder = bool(r.flags & FOLDER) and not r.flags & PACKAGE
            d = (display_text(r.name),
                 self.folder_text(r),
                 "" if folder else theme.format_size(r.size),
                 theme.format_date_short(r.mtime),
                 kinds.kind_label(r.name, r.flags))
            self._disp[row] = d
        return d

    # -- Qt model API
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=_DISPLAY):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == _DISPLAY:
            return HEADERS[section]
        if role == _ALIGNMENT:
            return _ALIGN_RIGHT if section == SIZE else _ALIGN_LEFT
        return None

    def data(self, index, role=_DISPLAY):
        if role in _UNUSED_ROLES:
            return None
        row = index.row()
        if not 0 <= row < len(self.rows):
            return None
        col = index.column()
        if role == _DISPLAY:
            return self._display(row)[col]
        r = self.rows[row]
        if role == _DECORATION:
            return self._icons.for_row(r.name, r.flags) if col == NAME else None
        if role == _ALIGNMENT:
            return _ALIGN_RIGHT if col == SIZE else _ALIGN_LEFT
        if role == _FONT:
            return self._tabular if col in (SIZE, MODIFIED) else None
        if role == _FOREGROUND:
            return self._secondary if col in (FOLDER_COL, KIND) else None
        if role == RowRole:
            return r
        if role == PathRole:
            return r.path
        if role == _TOOLTIP:
            if col == NAME:
                return display_text(r.path) + ("\nFavorite" if r.favorite else "")
            if col == FOLDER_COL:
                return display_text(os.path.dirname(r.path))
            if col == SIZE and not r.flags & FOLDER:
                return theme.format_size_exact(r.size)
            if col == MODIFIED:
                return theme.format_date_full(r.mtime)
            return None
        if role == _ACCESSIBLE:
            if col == NAME:
                d = self._display(row)
                parts = [d[0], d[1] or "root folder"]
                if d[2]:
                    parts.append(d[2])
                if d[3]:
                    parts.append(d[3])
                text = ", ".join(parts)
                return text + (", favorite" if r.favorite else "")
            return None
        return None

    def flags(self, index):
        return _ITEM_FLAGS if index.isValid() else _NO_FLAGS

    def mimeTypes(self):  # noqa: N802
        return ["text/uri-list", "text/plain"]

    def mimeData(self, indexes):  # noqa: N802
        rows = sorted({i.row() for i in indexes if i.isValid()})
        paths = [self.rows[r].path for r in rows if r < len(self.rows)]
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        mime.setText("\n".join(paths))
        return mime

    def supportedDragActions(self):  # noqa: N802
        return Qt.DropAction.CopyAction | Qt.DropAction.LinkAction


# --------------------------------------------------------------------------- delegate
class ResultDelegate(QStyledItemDelegate):
    """Name column: kind icon and the name with matched spans on a faint accent fill.
    Fonts and metrics are cached per (font, device pixel ratio), not built per paint."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._metrics: Dict[Tuple[str, float], QFontMetrics] = {}
        self._margin: Optional[int] = None

    def _fm(self, font: QFont, dpr: float) -> QFontMetrics:
        key = (font.key(), dpr)
        fm = self._metrics.get(key)
        if fm is None:
            fm = QFontMetrics(font)
            self._metrics[key] = fm
        return fm

    def sizeHint(self, option, index):  # noqa: N802
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), option.fontMetrics.height() + SPACE_M))

    def paint(self, painter: QPainter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(_CE_ITEM, opt, painter, widget)

        rect = style.subElementRect(_SE_TEXT, opt, widget)
        if self._margin is None:
            self._margin = style.pixelMetric(_PM_FOCUS_H, None, widget) + 1
        margin = self._margin
        rect = rect.adjusted(margin, 0, -margin, 0)
        state = opt.state
        selected = bool(state & _SELECTED)
        group = _G_NORMAL if state & _ACTIVE else _G_INACTIVE
        pal = opt.palette
        color = pal.color(group, _R_HTEXT if selected else _R_TEXT)
        dpr = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        fm = self._fm(opt.font, dpr)
        row = index.data(RowRole)

        painter.save()
        r = rect
        star_w = 0
        if row is not None and row.favorite:
            star = glyph_icon("star", "text")
            s = 12
            star.paint(painter, QRect(r.right() - s, r.center().y() - s // 2, s, s),
                       Qt.AlignmentFlag.AlignCenter, QIcon.Mode.Selected if selected else QIcon.Mode.Normal)
            star_w = s + SPACE_S
        width = max(0, r.width() - star_w)
        shown = text
        if fm.horizontalAdvance(text) > width:
            shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, width)
        spans = row.spans if row is not None else ()
        if spans:
            keep = len(shown) - 1 if shown != text else len(shown)
            fill = theme.match_fill_color(pal)
            if selected:
                fill = pal.color(QPalette.ColorRole.HighlightedText)
                fill.setAlphaF(0.22)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            top = r.top() + (r.height() - fm.height()) / 2
            for s, e in spans:
                if s >= keep:
                    continue
                e = min(e, keep)
                x = r.left() + fm.horizontalAdvance(shown[:s])
                w = fm.horizontalAdvance(shown[s:e])
                painter.drawRoundedRect(QRectF(x - 1, top, w + 2, fm.height()), 2, 2)
        painter.setPen(color)
        painter.setFont(opt.font)
        painter.drawText(QRect(r.left(), r.top(), width, r.height()), _ALIGN_LEFT, shown)
        painter.restore()


# --------------------------------------------------------------------------- view
class ResultsView(QTreeView):
    """Enter opens, Ctrl/Cmd+Enter reveals, Esc and Up on the first row go back to the
    search field."""
    open_requested = Signal()
    reveal_requested = Signal()
    focus_search_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ResultsView")
        self.setAccessibleName("Search results")
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(False)
        self.setAllColumnsShowFocus(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Folder paths keep both ends; sizes and dates never elide in practice.
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setSortingEnabled(False)

    def keyPressEvent(self, event: QKeyEvent):  # noqa: N802
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if mods == Qt.KeyboardModifier.ControlModifier:
                self.reveal_requested.emit()
                event.accept()
                return
            if mods == Qt.KeyboardModifier.NoModifier:
                self.open_requested.emit()
                event.accept()
                return
        if key == Qt.Key.Key_Escape and mods == Qt.KeyboardModifier.NoModifier:
            self.focus_search_requested.emit()
            event.accept()
            return
        if (key == Qt.Key.Key_Up and mods == Qt.KeyboardModifier.NoModifier
                and self.currentIndex().row() <= 0):
            self.focus_search_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class SortButton(DropDownButton):
    sort_changed = Signal(object)          # SortSpec

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SortButton")
        self.setAutoRaise(True)
        self.spec = SortSpec()
        menu = QMenu(self)
        self.key_group = QActionGroup(menu)
        self.key_actions: Dict[str, QAction] = {}
        for key, label in SORT_LABELS.items():
            act = menu.addAction(label)
            act.setCheckable(True)
            self.key_group.addAction(act)
            act.triggered.connect(lambda _=False, k=key: self._choose(k, None))
            self.key_actions[key] = act
        menu.addSeparator()
        self.order_group = QActionGroup(menu)
        self.asc_action = menu.addAction("Ascending")
        self.desc_action = menu.addAction("Descending")
        for act, desc in ((self.asc_action, False), (self.desc_action, True)):
            act.setCheckable(True)
            self.order_group.addAction(act)
            act.triggered.connect(lambda _=False, d=desc: self._choose(None, d))
        self.setMenu(menu)
        self.set_spec(SortSpec(), emit=False)

    @staticmethod
    def label_for(spec: SortSpec) -> str:
        if spec.key == "relevance":
            return "Relevance"
        return f"{SORT_LABELS.get(spec.key, spec.key)} {'↓' if spec.descending else '↑'}"

    def _choose(self, key: Optional[str], desc: Optional[bool]) -> None:
        key = key or self.spec.key
        if desc is None:
            desc = self.spec.descending if key == self.spec.key else key in ("size", "mtime")
        if key == "relevance":
            desc = False
        self.set_spec(SortSpec(key, desc))

    def set_spec(self, spec: SortSpec, emit: bool = True) -> None:
        changed = spec != self.spec
        self.spec = spec
        self.setText(self.label_for(spec))
        self.setAccessibleName(f"Sort by {self.label_for(spec)}")
        self.setToolTip("Sort results")
        for key, act in self.key_actions.items():
            act.setChecked(key == spec.key)
        rel = spec.key == "relevance"
        self.asc_action.setEnabled(not rel)
        self.desc_action.setEnabled(not rel)
        self.asc_action.setChecked(not rel and not spec.descending)
        self.desc_action.setChecked(not rel and spec.descending)
        if emit and changed:
            self.sort_changed.emit(spec)


def format_ms(ms: float) -> str:
    if ms < 1:
        return "under 1 ms"
    if ms < 1000:
        return f"{round(ms):,} ms"
    return f"{ms / 1000:.1f} s"


def summary_text(total: int, ms: float, partial: bool = False) -> str:
    if total == 0:
        text = "No results"
    elif total == 1:
        text = f"1 result in {format_ms(ms)}"
    else:
        text = f"{theme.format_count(total)} results in {format_ms(ms)}"
    if partial:
        text += " · indexing, more may appear"
    return text


class ResultsPane(QWidget):
    """Header ("16 results in 15 ms" + sort) above the results list or an empty state."""
    sort_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ResultsPane")
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE_XS)
        head = QHBoxLayout()
        head.setContentsMargins(SPACE_XS, 0, 0, 0)
        head.setSpacing(SPACE_S)
        self.summary = QLabel("")
        self.summary.setObjectName("ResultsSummary")
        self.summary.setFont(theme.tabular_font(theme.ui_font(1.0, QFont.Weight.DemiBold)))
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setAccessibleName("Result count")
        head.addWidget(self.summary, 1)
        self.sort_button = SortButton()
        self.sort_button.sort_changed.connect(self._on_sort_button)
        head.addWidget(self.sort_button)
        self.summary.setMinimumHeight(self.sort_button.sizeHint().height())
        col.addLayout(head)

        self.stack = QStackedWidget()
        self.empty_state = EmptyState()
        self.model = ResultsModel(self)
        self.view = ResultsView()
        self.view.setModel(self.model)
        # Only the Name column needs Python painting; the others use Qt's C++ delegate.
        self.name_delegate = ResultDelegate(self.view)
        self.view.setItemDelegateForColumn(NAME, self.name_delegate)
        self.stack.addWidget(self.empty_state)
        self.stack.addWidget(self.view)
        col.addWidget(self.stack, 1)
        self._setup_header()
        self._error = False

    # -- header columns
    def _setup_header(self) -> None:
        header = self.view.header()
        header.setAccessibleName("Result columns")
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.setHighlightSections(False)
        header.sectionClicked.connect(self._on_header_clicked)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        self._apply_default_columns()
        self.default_header_state = header.saveState()
        state = theme.ui_settings().value("results/header_v3")
        self._fit_folder_pending = True
        self._fitting = False
        if state and header.restoreState(state):
            self._fit_folder_pending = False
        header.viewport().installEventFilter(self)
        self.view.viewport().installEventFilter(self)
        spec = theme.ui_settings().value("results/sort", "relevance")
        key, _, order = str(spec or "relevance").partition(":")
        if key not in SORT_LABELS:
            key = "relevance"
        self.sort_button.set_spec(SortSpec(key, order == "desc" and key != "relevance"), emit=False)
        self._sync_indicator()

    def _apply_default_columns(self) -> None:
        header = self.view.header()
        fm = QFontMetrics(self.view.font())
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # Name takes the spare width; Folder gets ~1/3 of the list once it has a size.
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        header.resizeSection(FOLDER_COL, fm.horizontalAdvance("x" * 28) + 40)
        header.resizeSection(SIZE, fm.horizontalAdvance("1,023 MB") + 2 * SPACE_M + 12)
        header.resizeSection(MODIFIED, fm.horizontalAdvance("0000-00-00 00:00") + 2 * SPACE_M + 12)
        header.resizeSection(KIND, fm.horizontalAdvance("JavaScript file") + 2 * SPACE_M)
        for c in range(len(HEADERS)):
            header.setSectionHidden(c, c == KIND)
            header.moveSection(header.visualIndex(c), c)

    def save_state(self) -> None:
        s = theme.ui_settings()
        s.setValue("results/header_v3", self.view.header().saveState())
        spec = self.sort_spec()
        s.setValue("results/sort", f"{spec.key}:{'desc' if spec.descending else 'asc'}")

    def _header_menu(self, pos) -> None:
        header = self.view.header()
        menu = QMenu(self)
        for c in (FOLDER_COL, SIZE, MODIFIED, KIND):
            act = menu.addAction(HEADERS[c])
            act.setCheckable(True)
            act.setChecked(not header.isSectionHidden(c))
            act.triggered.connect(lambda checked, c=c: header.setSectionHidden(c, not checked))
        menu.addSeparator()
        menu.addAction("Sort by relevance", lambda: self.set_sort(SortSpec()))
        menu.addAction("Reset columns", self.reset_columns)
        self._last_menu = menu
        menu.popup(header.mapToGlobal(pos))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.view.viewport() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._fit_folder()
        elif obj is self.view.header().viewport() and event.type() == QEvent.Type.MouseButtonPress:
            self._fit_folder_pending = False      # the user is dragging a column: hands off
        return super().eventFilter(obj, event)

    def _fit_folder(self) -> None:
        """Until the user resizes a column: Folder ~ 1/3 of the width, Name the rest."""
        width = self.view.viewport().width()
        if self._fit_folder_pending and width > 300 and self.view.isVisible():
            self._fitting = True
            try:
                self.view.header().resizeSection(FOLDER_COL, int(width * 0.33))
            finally:
                self._fitting = False

    def reset_columns(self) -> None:
        self.view.header().restoreState(self.default_header_state)
        self._fit_folder_pending = True
        self._fit_folder()
        self.set_sort(SortSpec())

    # -- sorting
    def sort_spec(self) -> SortSpec:
        return self.sort_button.spec

    def set_sort(self, spec: SortSpec) -> None:
        self.sort_button.set_spec(spec)          # emits sort_changed when different

    def _on_sort_button(self, spec: SortSpec) -> None:
        self._sync_indicator()
        self.sort_changed.emit(spec)

    def _on_header_clicked(self, col: int) -> None:
        key = COLUMN_SORT.get(col)
        if key is None:
            return
        cur = self.sort_spec()
        if cur.key == key:
            spec = SortSpec(key, not cur.descending)
        else:
            spec = SortSpec(key, key in ("size", "mtime"))
        self.set_sort(spec)

    def _sync_indicator(self) -> None:
        header = self.view.header()
        spec = self.sort_spec()
        col = next((c for c, k in COLUMN_SORT.items() if k == spec.key), None)
        if col is None:
            header.setSortIndicatorShown(False)
            header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        else:
            header.setSortIndicatorShown(True)
            header.setSortIndicator(col, Qt.SortOrder.DescendingOrder if spec.descending
                                    else Qt.SortOrder.AscendingOrder)

    # -- header text
    def set_summary(self, text: str, error: bool = False) -> None:
        self.summary.setText(text)
        self.summary.setAccessibleDescription(text)
        if error != self._error or error:
            pal = self.summary.palette()
            if error:
                pal.setColor(QPalette.ColorRole.WindowText, theme.error_text_color())
            else:
                pal = QApplication.palette()
            self.summary.setPalette(pal)
            self._error = error

    # -- stack
    def show_empty(self, kind: str, title: str, message: str = "", actions=(), link: str = "") -> None:
        self.empty_state.set_content(kind, title, message, actions, link)
        self.stack.setCurrentWidget(self.empty_state)
        self.sort_button.setVisible(kind == "no-matches")

    def show_results(self) -> None:
        self.stack.setCurrentWidget(self.view)
        self.sort_button.setVisible(True)

    def is_showing_results(self) -> bool:
        return self.stack.currentWidget() is self.view

    # -- selection
    def current_row(self) -> Optional[ResultRow]:
        idx = self.view.currentIndex()
        if idx.isValid() and self.view.selectionModel().isRowSelected(idx.row(), QModelIndex()):
            return self.model.row(idx.row())
        rows = self.selected_rows()
        return rows[0] if rows else None

    def selected_rows(self) -> List[ResultRow]:
        sel = sorted(i.row() for i in self.view.selectionModel().selectedRows(0))
        return [r for r in (self.model.row(i) for i in sel) if r is not None]

    def selected_paths(self) -> List[str]:
        return [r.path for r in self.selected_rows()]

    def select_row(self, row: int) -> None:
        if not 0 <= row < self.model.rowCount():
            return
        idx = self.model.index(row, 0)
        self.view.selectionModel().setCurrentIndex(
            idx, self.view.selectionModel().SelectionFlag.ClearAndSelect
            | self.view.selectionModel().SelectionFlag.Rows)
        self.view.scrollTo(idx)

    def move_selection(self, key: int, ctrl: bool) -> bool:
        """Up/Down/PageUp/PageDown/Ctrl+Home/End from the search field."""
        n = self.model.rowCount()
        if not n or not self.is_showing_results():
            return False
        cur = self.view.currentIndex().row() if self.view.currentIndex().isValid() else -1
        page = max(1, self.view.viewport().height() // max(1, self.view.sizeHintForRow(0) or 24) - 1)
        if key == Qt.Key.Key_Down:
            new = cur + 1 if cur >= 0 else 0
        elif key == Qt.Key.Key_Up:
            new = max(0, cur - 1)
        elif key == Qt.Key.Key_PageDown:
            new = (cur if cur >= 0 else 0) + page
        elif key == Qt.Key.Key_PageUp:
            new = max(0, cur - page)
        elif key == Qt.Key.Key_Home and ctrl:
            new = 0
        elif key == Qt.Key.Key_End and ctrl:
            new = n - 1
        else:
            return False
        new = min(new, n - 1)
        if new >= n - 1 and self.model.canFetchMore():
            self.model.fetchMore()
        self.select_row(new)
        return True
