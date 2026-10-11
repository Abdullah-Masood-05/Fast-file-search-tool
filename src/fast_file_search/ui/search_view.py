"""Search field, suggestions and the results list (model/view, fast with 10k+ rows)."""
from __future__ import annotations

import html
import os
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import (
    QAbstractItemModel, QAbstractTableModel, QEvent, QFileInfo, QMimeData, QModelIndex,
    QPointF, QRectF, QSize, QStringListModel, Qt, QThread, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QFont, QFontMetrics, QGuiApplication, QIcon, QKeyEvent, QPainter, QPalette,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCompleter, QFileDialog, QFileIconProvider, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QStackedWidget, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QToolButton, QTreeView, QVBoxLayout, QWidget,
)

from ..persistent_index import PersistentIndex
from ..search import SearchResponse, SearchResult
from ..search_history import Autocomplete, SearchHistory, SearchRecommender
from . import file_actions, theme
from .theme import SPACE_M, SPACE_S, SPACE_XS, display_text
from .widgets import EmptyState

# --------------------------------------------------------------------------- model
NAME, FOLDER, SIZE, MODIFIED, TYPE = range(5)
HEADERS = ("Name", "Folder", "Size", "Modified", "Type")

ResultRole = Qt.ItemDataRole.UserRole + 1
FavoriteRole = Qt.ItemDataRole.UserRole + 2
PathRole = Qt.ItemDataRole.UserRole + 3

# Enum members and flag combinations are resolved once: PySide6 builds them through Python's
# enum machinery, which is slow when done per cell (the view calls flags() for every row).
_DISPLAY = Qt.ItemDataRole.DisplayRole
_DECORATION = Qt.ItemDataRole.DecorationRole
_TOOLTIP = Qt.ItemDataRole.ToolTipRole
_ALIGNMENT = Qt.ItemDataRole.TextAlignmentRole
_FONT = Qt.ItemDataRole.FontRole
_FOREGROUND = Qt.ItemDataRole.ForegroundRole
_ACCESSIBLE = Qt.ItemDataRole.AccessibleTextRole
_ALIGN_RIGHT = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
_ALIGN_LEFT = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
_NO_FLAGS = Qt.ItemFlag.NoItemFlags
_ITEM_FLAGS = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
               | Qt.ItemFlag.ItemIsDragEnabled)
_DESCENDING = Qt.SortOrder.DescendingOrder

# PySide6 needs the two-argument overload of layout(AboutTo)BeChanged picked explicitly.
_LAYOUT_ARGS = ('QList<QPersistentModelIndex>', 'QAbstractItemModel::LayoutChangeHint')


def _type_label(r: SearchResult) -> str:
    if r.is_folder:
        return "Folder"
    ext = (r.extension or "").lstrip(".")
    return ext.upper() if ext else "File"


def match_ranges(text: str, terms: Sequence[str]) -> List[Tuple[int, int]]:
    """Merged ``(start, end)`` spans of ``text`` that contain any of ``terms`` (case-insensitive)."""
    lower = text.lower()
    if len(lower) != len(text):  # rare case-mapping that changes length: skip highlighting
        return []
    spans = []
    for term in terms:
        t = term.lower()
        if not t:
            continue
        start = lower.find(t)
        while start >= 0:
            spans.append((start, start + len(t)))
            start = lower.find(t, start + len(t))
    spans.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


class IconCache:
    """File-type icons from the platform, cached per extension (per file only for
    types whose icon is the file's own, such as .exe or .app)."""

    PER_FILE = {".exe", ".lnk", ".ico", ".url", ".app", ".appimage", ".desktop", ".icns"}
    MAX_PER_FILE = 2000

    def __init__(self):
        self.provider = QFileIconProvider()
        self._cache = {}
        self._per_file = 0

    def icon(self, r: SearchResult) -> QIcon:
        if r.is_folder:
            key = "<folder>"
        else:
            ext = (r.extension or "").lower()
            key = r.path if ext in self.PER_FILE and self._per_file < self.MAX_PER_FILE else ext or "<none>"
        icon = self._cache.get(key)
        if icon is None:
            if r.is_folder:
                icon = self.provider.icon(QFileIconProvider.IconType.Folder)
            else:
                icon = self.provider.icon(QFileInfo(r.path))
                if icon.isNull():
                    icon = self.provider.icon(QFileIconProvider.IconType.File)
            if key == r.path:
                self._per_file += 1
            self._cache[key] = icon
        return icon


class ResultsModel(QAbstractTableModel):
    """Search results.  Column -1 sorting means relevance (score, highest first)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all: List[SearchResult] = []
        self._rows: List[SearchResult] = []
        self._favorites: Set[str] = set()
        self.favorites_only = False
        self.highlight_terms: List[str] = []
        self._sort_col = -1
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._icons = IconCache()
        self._tabular = theme.tabular_font()

    # -- content
    def set_results(self, results: Iterable[SearchResult], favorites: Iterable[str] = (),
                    terms: Sequence[str] = ()) -> None:
        self.beginResetModel()
        self._all = sorted(results, key=lambda r: -float(r.score or 0.0))
        self._favorites = set(favorites)
        self.highlight_terms = [t.strip("*?") for t in terms if t and t.strip("*?")]
        self._rows = self._sorted(self._filtered())
        self.endResetModel()

    def clear(self) -> None:
        self.set_results([])

    def _filtered(self) -> List[SearchResult]:
        if self.favorites_only:
            return [r for r in self._all if r.path in self._favorites]
        return list(self._all)

    def set_favorites_only(self, on: bool) -> None:
        if on == self.favorites_only:
            return
        self.beginResetModel()
        self.favorites_only = on
        self._rows = self._sorted(self._filtered())
        self.endResetModel()

    def set_favorite(self, path: str, on: bool) -> None:
        if on:
            self._favorites.add(path)
        else:
            self._favorites.discard(path)
        if self.favorites_only and not on:
            self.remove_paths([path], keep_in_all=True)
            return
        for row, r in enumerate(self._rows):
            if r.path == path:
                self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def remove_paths(self, paths: Iterable[str], keep_in_all: bool = False) -> None:
        gone = set(paths)
        if not keep_in_all:
            self._all = [r for r in self._all if r.path not in gone]
        for row in range(len(self._rows) - 1, -1, -1):
            if self._rows[row].path in gone:
                self.beginRemoveRows(QModelIndex(), row, row)
                del self._rows[row]
                self.endRemoveRows()

    def total_count(self) -> int:
        return len(self._all)

    def result(self, row: int) -> Optional[SearchResult]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_of(self, path: str) -> int:
        for row, r in enumerate(self._rows):
            if r.path == path:
                return row
        return -1

    # -- sorting
    def _sort_key(self, col: int):
        if col == NAME:
            return lambda r: r.name.casefold()
        if col == FOLDER:
            return lambda r: (os.path.dirname(r.path).casefold(), r.name.casefold())
        if col == SIZE:
            return lambda r: (-1 if r.is_folder else r.size, r.name.casefold())
        if col == MODIFIED:
            return lambda r: (r.modified or 0, r.name.casefold())
        if col == TYPE:
            return lambda r: (not r.is_folder, (r.extension or "").lower(), r.name.casefold())
        return None

    def _sorted(self, rows: List[SearchResult]) -> List[SearchResult]:
        key = self._sort_key(self._sort_col)
        if key is None:
            pos = {id(r): i for i, r in enumerate(self._all)}
            return sorted(rows, key=lambda r: pos.get(id(r), 0))
        return sorted(rows, key=key, reverse=self._sort_order == _DESCENDING)

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self._sort_col = column
        self._sort_order = order
        hint = QAbstractItemModel.LayoutChangeHint.VerticalSortHint
        self.layoutAboutToBeChanged[_LAYOUT_ARGS].emit([], hint)
        old = self.persistentIndexList()
        objs = [self.result(i.row()) if i.isValid() else None for i in old]
        self._rows = self._sorted(self._rows)
        pos = {id(r): n for n, r in enumerate(self._rows)}
        new = [self.index(pos[id(r)], i.column()) if r is not None and id(r) in pos else QModelIndex()
               for i, r in zip(old, objs)]
        self.changePersistentIndexList(old, new)
        self.layoutChanged[_LAYOUT_ARGS].emit([], hint)

    # -- Qt model API
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if section == SIZE:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index, role=_DISPLAY):
        if not index.isValid():
            return None
        row = index.row()
        rows = self._rows
        if not 0 <= row < len(rows):
            return None
        r = rows[row]
        col = index.column()
        if role == _DISPLAY:
            if col == NAME:
                return display_text(r.name)
            if col == FOLDER:
                return display_text(os.path.dirname(r.path))
            if col == SIZE:
                return "" if r.is_folder else theme.format_size(r.size)
            if col == MODIFIED:
                return theme.format_date_short(r.modified)
            if col == TYPE:
                return _type_label(r)
        elif role == _DECORATION:
            if col == NAME:
                return self._icons.icon(r)
        elif role == _TOOLTIP:
            if col == NAME:
                fav = "\nFavorite" if r.path in self._favorites else ""
                return display_text(r.path) + fav
            if col == FOLDER:
                return display_text(os.path.dirname(r.path))
            if col == SIZE and not r.is_folder:
                return theme.format_size_exact(r.size)
            if col == MODIFIED:
                return theme.format_date_full(r.modified)
        elif role == _ALIGNMENT:
            return _ALIGN_RIGHT if col == SIZE else _ALIGN_LEFT
        elif role == _FONT:
            if col == SIZE or col == MODIFIED:
                return self._tabular
        elif role == _FOREGROUND:
            if col == FOLDER or col == TYPE:
                return theme.secondary_text_color()
        elif role == _ACCESSIBLE:
            if col == NAME:
                return display_text(r.name) + (", favorite" if r.path in self._favorites else "")
        elif role == ResultRole:
            return r
        elif role == FavoriteRole:
            return r.path in self._favorites
        elif role == PathRole:
            return r.path
        return None

    def flags(self, index):
        return _ITEM_FLAGS if index.isValid() else _NO_FLAGS

    def mimeTypes(self):  # noqa: N802
        return ["text/uri-list", "text/plain"]

    def mimeData(self, indexes):  # noqa: N802
        rows = sorted({i.row() for i in indexes if i.isValid()})
        paths = [self._rows[r].path for r in rows if r < len(self._rows)]
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        mime.setText("\n".join(paths))
        return mime

    def supportedDragActions(self):  # noqa: N802
        return Qt.DropAction.CopyAction | Qt.DropAction.LinkAction


# --------------------------------------------------------------------------- view
class ResultDelegate(QStyledItemDelegate):
    """Draws the name column: icon, name with the matched part in semibold on a faint
    accent tint, and a small star for favorites."""

    def sizeHint(self, option, index):  # noqa: N802
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), option.fontMetrics.height() + SPACE_S))

    @staticmethod
    def _star(cx: float, cy: float, r: float) -> QPolygonF:
        import math
        pts = []
        for i in range(10):
            rad = r if i % 2 == 0 else r * 0.45
            a = -math.pi / 2 + i * math.pi / 5
            pts.append(QPointF(cx + rad * math.cos(a), cy + rad * math.sin(a)))
        return QPolygonF(pts)

    def initStyleOption(self, option, index):  # noqa: N802
        super().initStyleOption(option, index)
        if index.column() == FOLDER:
            # The deepest folders are the informative part of a path.
            option.textElideMode = Qt.TextElideMode.ElideLeft

    def paint(self, painter, option, index):
        if index.column() != NAME:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, widget) + 1
        rect = rect.adjusted(margin, 0, -margin, 0)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        group = (QPalette.ColorGroup.Normal if opt.state & QStyle.StateFlag.State_Active
                 else QPalette.ColorGroup.Inactive)
        if not opt.state & QStyle.StateFlag.State_Enabled:
            group = QPalette.ColorGroup.Disabled
        text_color = opt.palette.color(group, QPalette.ColorRole.HighlightedText if selected
                                       else QPalette.ColorRole.Text)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if index.data(FavoriteRole):
            fm = QFontMetrics(opt.font)
            r = fm.height() * 0.32
            cx = rect.right() - r
            cy = rect.center().y() + 0.5
            star_color = text_color if selected else theme.secondary_text_color(opt.palette)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(star_color)
            painter.drawPolygon(self._star(cx, cy, r))
            rect = rect.adjusted(0, 0, -int(2 * r + SPACE_S), 0)

        model = index.model()
        terms = getattr(model, "highlight_terms", None) or []
        normal = QFont(opt.font)
        bold = QFont(opt.font)
        bold.setWeight(QFont.Weight.DemiBold)
        fm, bfm = QFontMetrics(normal), QFontMetrics(bold)

        spans = match_ranges(text, terms) if terms else []
        segments = self._segments(text, spans)
        width = sum((bfm if m else fm).horizontalAdvance(s) for s, m in segments)
        if width > rect.width():
            elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width() - 2))
            keep = len(elided) - 1 if elided.endswith("…") else len(elided)
            spans = [(s, min(e, keep)) for s, e in spans if s < keep]
            segments = self._segments(elided, spans)

        painter.setClipRect(rect)
        x = float(rect.left())
        baseline = rect.top() + (rect.height() - fm.height()) / 2 + fm.ascent()
        fill = theme.match_fill_color(opt.palette)
        for seg, matched in segments:
            metrics = bfm if matched else fm
            w = metrics.horizontalAdvance(seg)
            if matched and not selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                top = rect.top() + (rect.height() - fm.height()) / 2
                painter.drawRoundedRect(QRectF(x - 1, top, w + 2, fm.height()), 2, 2)
            painter.setFont(bold if matched else normal)
            painter.setPen(text_color)
            painter.drawText(QPointF(x, baseline), seg)
            x += w
        painter.restore()

    @staticmethod
    def _segments(text: str, spans: List[Tuple[int, int]]) -> List[Tuple[str, bool]]:
        out, pos = [], 0
        for s, e in spans:
            if s > pos:
                out.append((text[pos:s], False))
            if e > s:
                out.append((text[s:e], True))
            pos = max(pos, e)
        if pos < len(text):
            out.append((text[pos:], False))
        return out


class ResultsView(QTreeView):
    """Results list.  Enter opens, Ctrl/Cmd+Enter reveals, Up on the first row and Esc
    go back to the search field."""
    open_requested = Signal()
    reveal_requested = Signal()
    focus_search_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Search results")
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setAllColumnsShowFocus(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

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


# --------------------------------------------------------------------------- suggestions
class SuggestionWorkerThread(QThread):
    """Fetches autocomplete suggestions off the GUI thread."""
    suggestions_ready = Signal(str, object)  # query, suggestions

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


# --------------------------------------------------------------------------- search view
class SearchView(QWidget):
    """Search field (``search_bar``), suggestions, results list and empty states."""
    search_triggered = Signal(str)
    file_selected = Signal(str)
    status_message = Signal(str)
    cleared = Signal()
    hide_requested = Signal()
    selection_changed = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        history_mgr: Optional[SearchHistory] = None,
        persistent_index: Optional[PersistentIndex] = None,
    ):
        super().__init__(parent)
        self.history_mgr = history_mgr or SearchHistory()
        # Share the application's index when one is supplied.
        self.index = persistent_index
        self.autocomplete = Autocomplete(persistent_index)
        self.recommender = SearchRecommender(persistent_index)
        self.raw_results: List[SearchResult] = []
        self._last_searched = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_S)

        # -- search bar (MainWindow moves it into the window's top row)
        self.search_bar = QWidget()
        bar = QVBoxLayout(self.search_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(SPACE_XS)
        self.search_row = QHBoxLayout()
        self.search_row.setSpacing(SPACE_S)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search files and folders")
        self.search_input.setAccessibleName("Search")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFont(theme.ui_font(1.15))
        self.search_input.setMinimumHeight(self.search_input.sizeHint().height() + SPACE_XS)
        find_icon = QIcon.fromTheme("edit-find")
        if not find_icon.isNull():
            self.search_input.addAction(find_icon, QLineEdit.ActionPosition.LeadingPosition)
        self.search_input.textEdited.connect(self._on_text_edited)
        self.search_input.textChanged.connect(self._on_text_changed)
        self.search_input.installEventFilter(self)
        self.search_row.addWidget(self.search_input, 1)

        self.fav_btn = QToolButton()
        self.fav_btn.setText("Favorites")
        self.fav_btn.setCheckable(True)
        self.fav_btn.setToolTip("Show only results marked as favorites")
        self.fav_btn.setAccessibleName("Show only favorites")
        self.fav_btn.toggled.connect(self.toggle_fav_filter)
        self.search_row.addWidget(self.fav_btn)
        bar.addLayout(self.search_row)

        # "Did you mean" suggestion or a search error, under the field.
        self.dym_lbl = QLabel()
        self.dym_lbl.setVisible(False)
        self.dym_lbl.setWordWrap(True)
        self.dym_lbl.linkActivated.connect(self.apply_dym_correction)
        bar.addWidget(self.dym_lbl)
        layout.addWidget(self.search_bar)

        # -- suggestions (a parented QCompleter popup works on Wayland, unlike tooltips)
        self.suggestion_model = QStringListModel(self)
        self.completer = QCompleter(self.suggestion_model, self)
        self.completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completer.setMaxVisibleItems(6)
        self.completer.setWidget(self.search_input)
        self.completer.activated[str].connect(self.apply_suggestion)
        # Installed after QCompleter's own filter, so it sees keys first.
        self.completer.popup().installEventFilter(self)
        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(150)
        self.debounce_timer.timeout.connect(self._fetch_suggestions_debounced)
        self.suggestion_thread = SuggestionWorkerThread(self.autocomplete)
        self.suggestion_thread.suggestions_ready.connect(self._on_suggestions_ready)

        # -- results / empty state
        self.stack = QStackedWidget()
        self.empty_state = EmptyState()
        self.model = ResultsModel(self)
        self.results_view = ResultsView()
        self.results_view.setModel(self.model)
        self.results_view.setItemDelegate(ResultDelegate(self.results_view))
        self.stack.addWidget(self.empty_state)
        self.stack.addWidget(self.results_view)
        layout.addWidget(self.stack, 1)
        self._setup_header()

        self.results_view.doubleClicked.connect(lambda _i: self.open_selected())
        self.results_view.open_requested.connect(self.open_selected)
        self.results_view.reveal_requested.connect(self.reveal_selected)
        self.results_view.focus_search_requested.connect(self.focus_search)
        self.results_view.customContextMenuRequested.connect(self.open_context_menu)
        self.results_view.selectionModel().currentRowChanged.connect(self._on_current_changed)
        self.results_view.selectionModel().selectionChanged.connect(lambda *_: self.selection_changed.emit())
        self.model.modelReset.connect(self.selection_changed.emit)

        self.show_empty("ready", "Search by name", "")

        # Never let Qt destroy the suggestion QThread while it is still running.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    # ------------------------------------------------------------------ header
    def _setup_header(self) -> None:
        view = self.results_view
        header = view.header()
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicatorClearable(True)  # third click goes back to relevance
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self.open_header_menu)
        self.default_header_state = None
        self._apply_default_columns()
        self.default_header_state = header.saveState()
        state = theme.ui_settings().value("results/header")
        if state:
            header.restoreState(state)
        view.setSortingEnabled(True)

    def _apply_default_columns(self) -> None:
        header = self.results_view.header()
        fm = QFontMetrics(self.results_view.font())
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(FOLDER, QHeaderView.ResizeMode.Stretch)
        header.resizeSection(NAME, fm.horizontalAdvance("x" * 34) + 40)
        header.resizeSection(SIZE, fm.horizontalAdvance("1,023 MB") + 2 * SPACE_M + 12)
        header.resizeSection(MODIFIED, fm.horizontalAdvance("0000-00-00 00:00") + 2 * SPACE_M + 12)
        header.resizeSection(TYPE, fm.horizontalAdvance("Folder") + 2 * SPACE_M + 12)
        for col in range(len(HEADERS)):
            header.setSectionHidden(col, col == TYPE)
            header.moveSection(header.visualIndex(col), col)

    def save_state(self) -> None:
        theme.ui_settings().setValue("results/header", self.results_view.header().saveState())

    def open_header_menu(self, position):
        header = self.results_view.header()
        menu = QMenu(self)
        for col in (FOLDER, SIZE, MODIFIED, TYPE):
            act = QAction(HEADERS[col], menu)
            act.setCheckable(True)
            act.setChecked(not header.isSectionHidden(col))
            act.triggered.connect(lambda checked, c=col: header.setSectionHidden(c, not checked))
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction("Sort by relevance", self.sort_by_relevance)
        menu.addAction("Reset columns", self.reset_columns)
        menu.exec(header.mapToGlobal(position))

    def sort_by_relevance(self) -> None:
        self.results_view.header().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.model.sort(-1)

    def reset_columns(self) -> None:
        self.results_view.header().restoreState(self.default_header_state)
        self.sort_by_relevance()

    # ------------------------------------------------------------------ state
    def shutdown(self) -> None:
        """Stop background workers; safe to call more than once."""
        self.debounce_timer.stop()
        self.suggestion_thread.shutdown()

    def show_empty(self, kind: str, title: str, message: str = "", actions=()) -> None:
        self.empty_state.set_content(kind, title, message, actions)
        self.stack.setCurrentWidget(self.empty_state)

    def show_results(self) -> None:
        self.stack.setCurrentWidget(self.results_view)

    def is_showing_results(self) -> bool:
        return self.stack.currentWidget() is self.results_view

    def result_count(self) -> int:
        return self.model.rowCount()

    def focus_search(self, select_all: bool = True) -> None:
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        if select_all:
            self.search_input.selectAll()

    def focus_results(self) -> bool:
        if not self.is_showing_results() or self.model.rowCount() == 0:
            return False
        view = self.results_view
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        if not view.currentIndex().isValid() or not view.selectionModel().hasSelection():
            view.setCurrentIndex(self.model.index(0, 0))
        return True

    def clear_search(self):
        self.debounce_timer.stop()
        self.search_input.clear()
        self.model.clear()
        self.raw_results = []
        self.dym_lbl.setVisible(False)
        self.completer.popup().hide()
        self.cleared.emit()

    def set_results(self, response: SearchResponse, terms: Optional[Sequence[str]] = None):
        """Show a search response (results, "did you mean", or an error)."""
        self.raw_results = list(response.results)
        favorites = self.history_mgr.get_favorites()
        if terms is None:
            terms = sorted({m for r in response.results for m in (r.matches or []) if isinstance(m, str)})
        self.model.set_results(self.raw_results, favorites, terms)
        self.results_view.scrollToTop()

        error = getattr(response, "error", None)
        if error:
            self._set_note(error, error=True)
        elif response.did_you_mean:
            # A file name: escape it before it goes into rich text and an href.
            safe = html.escape(response.did_you_mean, quote=True)
            self._set_note(f"Did you mean <a href=\"{safe}\">{safe}</a>?", rich=True)
        else:
            self.dym_lbl.setVisible(False)

        if self.model.rowCount():
            self.show_results()
        elif self.model.total_count() and self.model.favorites_only:
            self.show_empty("no-favorites", "No favorites in these results",
                            "Turn off Favorites to see all results.",
                            [("Show all results", lambda: self.fav_btn.setChecked(False))])
        else:
            query = self.search_input.text().strip()
            self.show_empty("no-matches", f"No files match “{display_text(query)}”"
                            if query else "No matches", "Check the filters or the indexed folders.")

    def _set_note(self, text: str, error: bool = False, rich: bool = False) -> None:
        theme.set_error_text(self.dym_lbl, error)
        self.dym_lbl.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        self.dym_lbl.setText(text)
        self.dym_lbl.setVisible(True)

    def toggle_fav_filter(self, checked: Optional[bool] = None):
        on = self.fav_btn.isChecked() if checked is None else bool(checked)
        self.model.set_favorites_only(on)
        if self.raw_results or self.model.rowCount():
            if self.model.rowCount():
                self.show_results()
            else:
                self.show_empty("no-favorites", "No favorites in these results",
                                "Turn off Favorites to see all results.",
                                [("Show all results", lambda: self.fav_btn.setChecked(False))])

    @property
    def fav_filter_active(self) -> bool:
        return self.model.favorites_only

    # ------------------------------------------------------------------ suggestions
    def apply_suggestion(self, text):
        self.debounce_timer.stop()
        self.completer.popup().hide()
        self.search_input.setText(text)
        self._emit_search(text)

    def apply_dym_correction(self, text):
        self.debounce_timer.stop()
        self.search_input.setText(text)
        self._emit_search(text)

    def _emit_search(self, text: str) -> None:
        self._last_searched = text.strip()
        self.search_triggered.emit(text.strip())

    def _on_text_edited(self, text):
        if not text.strip():
            self.completer.popup().hide()
            self.debounce_timer.stop()
            return
        self.debounce_timer.start()

    def _on_text_changed(self, text):
        if not text:
            self.completer.popup().hide()
            self.debounce_timer.stop()

    def _fetch_suggestions_debounced(self):
        text = self.search_input.text().strip()
        if text:
            self.suggestion_thread.fetch(text)
        else:
            self.completer.popup().hide()

    @Slot(str, object)
    def _on_suggestions_ready(self, query: str, suggestions: list):
        current = self.search_input.text().strip()
        if (not current or current != query or query == self._last_searched
                or not self.search_input.hasFocus()):
            return
        suggestions = [s for s in suggestions if s != current]
        if suggestions:
            self.suggestion_model.setStringList([display_text(s) for s in suggestions])
            self.completer.complete()
            # Nothing highlighted: Enter searches what was typed, Down picks a suggestion.
            self.completer.popup().setCurrentIndex(QModelIndex())
        else:
            self.completer.popup().hide()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        completer = getattr(self, "completer", None)  # filters run during construction too
        if (completer is not None and obj is completer.popup() and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return)
                and not obj.currentIndex().isValid()):
            # Enter with no suggestion highlighted searches what was typed.
            obj.hide()
            self.debounce_timer.stop()
            self._emit_search(self.search_input.text())
            return True
        if (completer is not None and obj is self.search_input
                and event.type() == QEvent.Type.KeyPress):
            key = event.key()
            mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return) and mods == Qt.KeyboardModifier.NoModifier:
                # A pending debounce would pop the suggestions up again after the search.
                self.debounce_timer.stop()
                self.completer.popup().hide()
                self._emit_search(self.search_input.text())
                return True
            if key == Qt.Key.Key_Down and mods == Qt.KeyboardModifier.NoModifier:
                if self.focus_results():
                    return True
            if key == Qt.Key.Key_Escape and mods == Qt.KeyboardModifier.NoModifier:
                if self.search_input.text():
                    self.clear_search()
                else:
                    self.hide_requested.emit()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ selection
    def _on_current_changed(self, current: QModelIndex, _previous=None):
        path = current.data(PathRole) if current.isValid() else None
        if path:
            self.file_selected.emit(path)

    def current_path(self) -> Optional[str]:
        idx = self.results_view.currentIndex()
        if idx.isValid() and self.results_view.selectionModel().isRowSelected(idx.row(), QModelIndex()):
            return idx.data(PathRole)
        paths = self.selected_paths()
        return paths[0] if paths else None

    def selected_paths(self) -> List[str]:
        rows = sorted(i.row() for i in self.results_view.selectionModel().selectedRows(0))
        return [self.model.result(r).path for r in rows if self.model.result(r) is not None]

    def select_path(self, path: str) -> bool:
        row = self.model.row_of(path)
        if row < 0:
            return False
        self.results_view.setCurrentIndex(self.model.index(row, 0))
        return True

    # ------------------------------------------------------------------ actions
    def _report(self, errors: Iterable[Optional[str]]) -> None:
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
        action = box.addButton(action_text, QMessageBox.ButtonRole.DestructiveRole
                               if not default_to_action else QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(action if default_to_action else cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is action

    def open_selected(self, paths: Optional[List[str]] = None):
        paths = paths if paths is not None else self.selected_paths()
        if not paths:
            return
        if len(paths) > 10 and not self._confirm(
                "Open files", f"Open {len(paths)} items?", "Each one opens in its own app or window.",
                "Open"):
            return
        self._report(file_actions.open_path(p) for p in paths)

    def reveal_selected(self, path: Optional[str] = None):
        path = path or self.current_path()
        if path:
            self._report([file_actions.reveal(path)])

    def reveal_path(self, path):
        self.reveal_selected(path)

    def open_with_selected(self, path: Optional[str] = None, application: Optional[str] = None):
        path = path or self.current_path()
        if not path:
            return
        mode = file_actions.open_with_mode()
        if mode == "choose-app" and not application:
            application, _ = QFileDialog.getOpenFileName(self, "Choose an app", "/Applications",
                                                         "Apps (*.app)")
            if not application:
                return
        self._report([file_actions.open_with(path, application)])

    def copy_paths(self, paths: Optional[List[str]] = None):
        paths = paths if paths is not None else self.selected_paths()
        if paths:
            QGuiApplication.clipboard().setText("\n".join(paths))
            self.status_message.emit("Copied path" if len(paths) == 1 else f"Copied {len(paths)} paths")

    def copy_names(self, paths: Optional[List[str]] = None):
        paths = paths if paths is not None else self.selected_paths()
        if paths:
            QGuiApplication.clipboard().setText("\n".join(Path(p).name for p in paths))
            self.status_message.emit("Copied name" if len(paths) == 1 else f"Copied {len(paths)} names")

    def copy_to_clipboard(self, text):
        QGuiApplication.clipboard().setText(text)

    def toggle_favorite(self, path: str):
        if self.history_mgr.is_favorite(path):
            self.history_mgr.remove_favorite(path)
            self.model.set_favorite(path, False)
        else:
            self.history_mgr.add_favorite(path)
            self.model.set_favorite(path, True)

    def manage_tags(self, path):
        existing = ", ".join(self.history_mgr.get_tags(path))
        text, ok = QInputDialog.getText(
            self, "Tags", f"Tags for “{display_text(Path(path).name)}”, separated by commas:",
            QLineEdit.EchoMode.Normal, existing)
        if ok:
            for t in self.history_mgr.get_tags(path):
                self.history_mgr.remove_tag(path, t)
            for nt in (t.strip() for t in text.split(",")):
                if nt:
                    self.history_mgr.add_tag(path, nt)

    def manage_note(self, path):
        existing = self.history_mgr.get_note(path)
        text, ok = QInputDialog.getMultiLineText(
            self, "Note", f"Note for “{display_text(Path(path).name)}”:", existing)
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

    def _forget(self, paths: List[str]) -> None:
        if not paths:
            return
        gone = set(paths)
        self.model.remove_paths(paths)
        self.raw_results = [r for r in self.raw_results if r.path not in gone]
        for p in paths:
            self._forget_in_index(p)

    def trash_paths(self, paths: Optional[List[str]] = None):
        """Move to the Recycle Bin/Trash after confirming.  Permanent deletion only
        when the location has no trash, and only after a second, explicit confirmation."""
        paths = paths if paths is not None else self.selected_paths()
        if not paths:
            return
        n = len(paths)
        what = f"“{display_text(Path(paths[0]).name)}”" if n == 1 else f"{n} items"
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
        if no_trash:
            m = len(no_trash)
            what = f"“{display_text(Path(no_trash[0]).name)}”" if m == 1 else f"{m} items"
            if self._confirm("Delete permanently", f"{what} can’t be moved to {theme.trash_name()}.",
                             f"Delete {'it' if m == 1 else 'them'} permanently? This can’t be undone.",
                             "Delete permanently", default_to_action=False):
                for p in no_trash:
                    err = file_actions.delete_permanently(p)
                    if err:
                        errors.append(err)
                    else:
                        moved.append(p)
        self._forget(moved)
        if moved:
            k = len(moved)
            self.status_message.emit(f"Moved {k} {'item' if k == 1 else 'items'} to {theme.trash_name()}"
                                     if not no_trash else f"Deleted {k} {'item' if k == 1 else 'items'}")
        self._report(errors)

    # ------------------------------------------------------------------ context menu
    def build_context_menu(self, paths: List[str]) -> QMenu:
        menu = QMenu(self)
        path = paths[0]
        many = len(paths) > 1
        menu.addAction("Open", lambda: self.open_selected(paths))
        mode = file_actions.open_with_mode()
        if not many and mode in ("system", "choose-app"):
            menu.addAction("Open with…", lambda: self.open_with_selected(path))
        elif not many and mode == "list":
            sub = menu.addMenu("Open with")
            sub.aboutToShow.connect(lambda: self._fill_open_with(sub, path))
        reveal = menu.addAction(theme.reveal_label(), lambda: self.reveal_selected(path))
        reveal.setEnabled(not many)
        menu.addSeparator()
        menu.addAction("Copy paths" if many else "Copy path", lambda: self.copy_paths(paths))
        menu.addAction("Copy names" if many else "Copy name", lambda: self.copy_names(paths))
        if not many:
            menu.addSeparator()
            fav = self.history_mgr.is_favorite(path)
            menu.addAction("Remove from favorites" if fav else "Add to favorites",
                           lambda: self.toggle_favorite(path))
            menu.addAction("Edit tags…", lambda: self.manage_tags(path))
            menu.addAction("Edit note…", lambda: self.manage_note(path))
        menu.addSeparator()
        menu.addAction(theme.trash_label(), lambda: self.trash_paths(paths))
        return menu

    def _fill_open_with(self, menu: QMenu, path: str) -> None:
        if menu.actions():
            return
        choices = file_actions.open_with_choices(path)
        if not choices:
            act = menu.addAction("No apps found")
            act.setEnabled(False)
        for name, app in choices:
            menu.addAction(name, lambda a=app: self.open_with_selected(path, a))

    def open_context_menu(self, position):
        view = self.results_view
        index = view.indexAt(position)
        if not index.isValid():
            return
        if not view.selectionModel().isRowSelected(index.row(), QModelIndex()):
            view.setCurrentIndex(index)
        paths = self.selected_paths()
        if paths:
            self.build_context_menu(paths).exec(view.viewport().mapToGlobal(position))
