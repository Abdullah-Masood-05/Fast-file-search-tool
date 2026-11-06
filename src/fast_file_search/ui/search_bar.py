"""Search field, shortcut hint, Search button and the suggestion overlay (design E.2).

The bar never searches by itself: it emits ``search_requested(text, commit)`` after
the 120 ms debounce (``commit=False``) or on Enter / the Search button
(``commit=True``), and the main window turns that into one engine request.
"""
from __future__ import annotations

import time
from typing import List, Optional, Sequence

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QIcon, QKeyEvent, QKeySequence, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView,
    QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QAbstractListModel

from . import theme
from .theme import SPACE_M, SPACE_S, SPACE_XS, display_text
from .widgets import glyph_icon

DEBOUNCE_MS = 120
MAX_ROWS = 6

_NAV_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)


def shortcut_text(seq: str = "Ctrl+K") -> str:
    """``Ctrl+K`` on Windows/Linux, ``⌘K`` on macOS (Qt maps Ctrl to Cmd there)."""
    return QKeySequence(seq).toString(QKeySequence.SequenceFormat.NativeText)


def is_regex_query(text: str) -> bool:
    t = text.lstrip().lower()
    if t.startswith("case:"):
        t = t[5:]
    return t.startswith("re:") or t.startswith("regex:")


# --------------------------------------------------------------------------- field
class ShortcutHint(QLabel):
    """Right-aligned "Ctrl+K" inside the field, in the secondary colour."""

    def __init__(self, parent: QWidget):
        super().__init__(shortcut_text("Ctrl+K"), parent)
        self.setObjectName("ShortcutHint")
        theme.make_secondary(self)
        self.setFont(theme.ui_font(0.9))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAccessibleName("Keyboard shortcut")
        self.setAccessibleDescription(f"Press {self.text()} to search")


class SearchField(QLineEdit):
    """The query field. Keys that belong to the results list or the suggestion popup
    are forwarded as signals so focus stays here."""
    nav_key = Signal(QKeyEvent)          # Up/Down/PageUp/PageDown/Ctrl+Home/End
    enter_pressed = Signal()
    escape_pressed = Signal()
    tab_pressed = Signal()               # only consumed while the popup has a highlight
    copy_requested = Signal()            # Ctrl+C with no text selected
    focus_lost = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SearchField")
        self.setPlaceholderText("Search files and folders")
        self.setAccessibleName("Search")
        self.setAccessibleDescription(f"Type a file name. {shortcut_text('Ctrl+K')}")
        self.setClearButtonEnabled(True)
        self.setFont(theme.ui_font(1.1))
        self.preedit_active = False
        self.tab_handler = None          # callable -> bool (accepted)
        icon = QIcon.fromTheme(QIcon.ThemeIcon.EditFind) if hasattr(QIcon, "ThemeIcon") else QIcon()
        if icon.isNull():
            icon = QIcon.fromTheme("edit-find")
        if icon.isNull():
            icon = glyph_icon("search")
        self.search_action = self.addAction(icon, QLineEdit.ActionPosition.LeadingPosition)
        self.search_action.setToolTip("Search")
        self.search_action.setText("Search")
        self._name_side_buttons()
        self.hint = ShortcutHint(self)
        self.textChanged.connect(self._place_hint)
        self.setMinimumHeight(self.sizeHint().height() + SPACE_S)

    def _name_side_buttons(self) -> None:
        """Qt's own icon buttons inside the field (search icon, clear) get names too."""
        from PySide6.QtWidgets import QToolButton

        for btn in self.findChildren(QToolButton):
            if not btn.accessibleName():
                btn.setAccessibleName("Search" if btn.defaultAction() is self.search_action else "Clear search")

    # -- hint placement
    def _clear_button_width(self) -> int:
        if not self.text():
            return 0
        for child in self.children():
            name = type(child).__name__
            if name.endswith("IconButton") and isinstance(child, QWidget) and child.isVisible():
                return child.width() + SPACE_XS
        return self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize) + SPACE_S

    def _place_hint(self, *_args) -> None:
        self._name_side_buttons()
        hint = self.hint
        size = hint.sizeHint()
        right = self.width() - SPACE_S - self._clear_button_width()
        hint.setGeometry(right - size.width(), (self.height() - size.height()) // 2,
                         size.width(), size.height())
        self.setTextMargins(0, 0, size.width() + SPACE_S, 0)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._place_hint()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._place_hint)

    # -- input
    def inputMethodEvent(self, event):  # noqa: N802
        self.preedit_active = bool(event.preeditString())
        super().inputMethodEvent(event)

    def focusOutEvent(self, event):  # noqa: N802
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self.focus_lost.emit()

    def event(self, ev):  # noqa: D401 - Qt override
        # Tab normally moves focus before keyPressEvent sees it.
        if ev.type() == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Tab \
                and ev.modifiers() == Qt.KeyboardModifier.NoModifier:
            if self.tab_handler is not None and self.tab_handler():
                ev.accept()
                return True
        return super().event(ev)

    def keyPressEvent(self, event: QKeyEvent):  # noqa: N802
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        none = mods == Qt.KeyboardModifier.NoModifier
        ctrl = mods == Qt.KeyboardModifier.ControlModifier
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and none:
            self.enter_pressed.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape and none:
            self.escape_pressed.emit()
            event.accept()
            return
        if (key in _NAV_KEYS and none) or (key in (Qt.Key.Key_Home, Qt.Key.Key_End) and ctrl):
            self.nav_key.emit(event)
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Copy) and not self.hasSelectedText():
            self.copy_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------------------- popup
class SuggestionModel(QAbstractListModel):
    KindRole = Qt.ItemDataRole.UserRole + 1
    CountRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: List[tuple] = []     # protocol.Suggestion (kind, text, count)

    def set_items(self, items: Sequence[tuple]) -> None:
        self.beginResetModel()
        self.items = list(items)[:MAX_ROWS]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        s = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return display_text(s.text)
        if role == self.KindRole:
            return s.kind
        if role == self.CountRole:
            return s.count
        if role == Qt.ItemDataRole.AccessibleTextRole:
            what = "recent search" if s.kind == "history" else "name"
            return f"{display_text(s.text)}, {what}, {_count_text(s.count)}"
        return None


def _count_text(n: int) -> str:
    return f"{theme.format_count(n)} {'result' if n == 1 else 'results'}"


class SuggestionDelegate(QStyledItemDelegate):
    """Icon, text and a right-aligned secondary count."""

    def __init__(self, icon_for, parent=None):
        super().__init__(parent)
        self.icon_for = icon_for

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(option.rect.width(), option.fontMetrics.height() + SPACE_M)

    def paint(self, painter: QPainter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        opt.icon = QIcon()
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        rect = opt.rect.adjusted(SPACE_S, 0, -SPACE_S, 0)
        kind = index.data(SuggestionModel.KindRole)
        icon = glyph_icon("clock") if kind == "history" else self.icon_for(text)
        isz = 16
        icon.paint(painter, QRect(rect.left(), rect.center().y() - isz // 2 + 1, isz, isz),
                   Qt.AlignmentFlag.AlignCenter, QIcon.Mode.Selected if selected else QIcon.Mode.Normal)
        rect.setLeft(rect.left() + isz + SPACE_S)
        count = index.data(SuggestionModel.CountRole) or 0
        pal = opt.palette
        fg = pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        sec = fg if selected else theme.secondary_text_color(pal)
        fm = opt.fontMetrics
        count_s = _count_text(count) if (kind == "history" or count > 1) else ""
        cw = fm.horizontalAdvance(count_s) if count_s else 0
        painter.save()
        if count_s:
            painter.setPen(sec)
            painter.setFont(theme.tabular_font(opt.font))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), count_s)
        painter.setPen(fg)
        painter.setFont(opt.font)
        tr = rect.adjusted(0, 0, -(cw + SPACE_M if cw else 0), 0)
        painter.drawText(tr, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         fm.elidedText(text, Qt.TextElideMode.ElideRight, tr.width()))
        painter.restore()


class SuggestionPopup(QFrame):
    """In-window overlay under the search field. It never takes focus: the field keeps
    the keyboard and forwards Up/Down/Enter/Tab/Esc here."""
    accepted = Signal(object)            # protocol.Suggestion

    def __init__(self, host: QWidget, icon_for):
        super().__init__(host)
        self.setObjectName("SuggestionPopup")
        self.setFrameShape(QFrame.Shape.NoFrame)     # the 1 px border is painted in paintEvent
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Suggestions")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, SPACE_XS, 1, SPACE_XS)
        self.model = SuggestionModel(self)
        self.view = QListView(self)
        self.view.setObjectName("SuggestionList")
        self.view.setAccessibleName("Suggestions")
        self.view.setModel(self.model)
        self.view.setItemDelegate(SuggestionDelegate(icon_for, self.view))
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setUniformItemSizes(True)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setMouseTracking(True)
        self.view.clicked.connect(self._on_clicked)
        lay.addWidget(self.view)
        self.hide()

    @property
    def items(self) -> List[tuple]:
        return self.model.items

    def highlighted(self) -> int:
        idx = self.view.currentIndex()
        return idx.row() if idx.isValid() and self.view.selectionModel().isSelected(idx) else -1

    def set_items(self, items: Sequence[tuple]) -> None:
        self.model.set_items(items)
        self.view.clearSelection()
        self.view.setCurrentIndex(QModelIndex())

    def move_highlight(self, delta: int) -> None:
        n = self.model.rowCount()
        if not n:
            return
        cur = self.highlighted()
        new = (0 if delta > 0 else -1) if cur < 0 else cur + delta
        if new < 0 or new >= n:
            self.view.clearSelection()
            self.view.setCurrentIndex(QModelIndex())
            return
        idx = self.model.index(new, 0)
        self.view.setCurrentIndex(idx)
        self.view.selectionModel().select(idx, self.view.selectionModel().SelectionFlag.ClearAndSelect)

    def accept_highlighted(self) -> bool:
        row = self.highlighted()
        if row < 0:
            return False
        self.accepted.emit(self.model.items[row])
        return True

    def _on_clicked(self, index: QModelIndex) -> None:
        if index.isValid():
            self.accepted.emit(self.model.items[index.row()])

    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        super().paintEvent(event)
        # Palette-derived hairline: Mid is a step darker than Window in light themes and a
        # step lighter than Base in dark ones, so it reads on both without a shadow.
        p = QPainter(self)
        p.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()

    def place(self, field: QWidget) -> None:
        host = self.parentWidget()
        pos = field.mapTo(host, QPoint(0, field.height() + 2))
        rows = max(1, self.model.rowCount())
        row_h = self.view.sizeHintForRow(0) if self.model.rowCount() else field.fontMetrics().height() + SPACE_M
        h = rows * row_h + 2 * SPACE_XS + 2
        w = max(field.width(), 360)
        w = min(w, host.width() - pos.x() - SPACE_S)
        self.setGeometry(pos.x(), pos.y(), w, h)
        self.raise_()


# --------------------------------------------------------------------------- bar
class SearchBar(QWidget):
    """Search field + Search button + suggestion overlay. See the module docstring."""
    search_requested = Signal(str, bool)     # text, commit_history
    open_requested = Signal()                # Enter with nothing pending
    nav_key = Signal(QKeyEvent)
    escape_to_hide = Signal()                # Esc on an empty field
    cleared = Signal()
    copy_requested = Signal()
    keystroke = Signal()                     # any edit (the user typed)
    history_wanted = Signal()                # empty field focused via Ctrl/Cmd+K

    def __init__(self, overlay_host: QWidget, icon_for=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SearchBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_S)
        self.field = SearchField()
        row.addWidget(self.field, 1)
        self.search_btn = QPushButton("Search")
        self.search_btn.setAutoDefault(False)
        self.search_btn.setDefault(False)
        self.search_btn.setAccessibleName("Search")
        self.search_btn.setMinimumHeight(self.field.minimumHeight())
        row.addWidget(self.search_btn)

        self.popup = SuggestionPopup(overlay_host, icon_for or (lambda _t: glyph_icon("file")))
        self.popup.accepted.connect(self.accept_suggestion)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(DEBOUNCE_MS)
        self.timer.timeout.connect(self._fire_debounced)

        self.last_sent_text: Optional[str] = None   # text of the latest request
        self.results_text: Optional[str] = None     # text of the results on screen
        self._dismissed_for: Optional[str] = None
        self._selection_moved = False
        self.last_handler_ms = 0.0

        f = self.field
        f.textEdited.connect(self._on_text_edited)
        f.textChanged.connect(self._on_text_changed)
        f.enter_pressed.connect(self._on_enter)
        f.escape_pressed.connect(self._on_escape)
        f.nav_key.connect(self._on_nav)
        f.copy_requested.connect(self.copy_requested)
        f.focus_lost.connect(self.hide_popup)
        f.tab_handler = self._on_tab
        f.installEventFilter(self)
        self.search_btn.clicked.connect(self.flush)

    # -- public
    def text(self) -> str:
        return self.field.text()

    def set_text(self, text: str) -> None:
        self.timer.stop()
        self.field.setText(text)

    def is_pending(self) -> bool:
        return self.timer.isActive() or self.field.text() != self.last_sent_text

    def mark_sent(self, text: str) -> None:
        self.last_sent_text = text

    def flush(self) -> None:
        """Search now and record history (Enter, the Search button)."""
        self.timer.stop()
        self.hide_popup()
        self.search_requested.emit(self.field.text(), True)

    def note_selection_moved(self) -> None:
        self._selection_moved = True
        self.hide_popup()

    def show_suggestions(self, text: str, suggestions: Sequence[tuple]) -> None:
        """Called by the window with the suggestions of the current gen's result."""
        f = self.field
        items = [s for s in suggestions if s.text != text]
        if (not items or not f.hasFocus() or self._selection_moved or text != f.text()
                or self._dismissed_for == text or is_regex_query(text) or self.timer.isActive()):
            if not items or text == f.text():
                self.hide_popup()
            return
        self.popup.set_items(items)
        self.popup.place(f)
        self.popup.show()
        self.popup.raise_()

    def hide_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.hide()

    def popup_visible(self) -> bool:
        return self.popup.isVisible()

    def focus(self, select_all: bool = True) -> None:
        self.field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        if select_all:
            self.field.selectAll()
        if not self.field.text():
            self._dismissed_for = None
            self._selection_moved = False
            self.history_wanted.emit()

    def accept_suggestion(self, suggestion) -> None:
        self.hide_popup()
        self.timer.stop()
        self.field.setText(suggestion.text)
        self._dismissed_for = suggestion.text
        self.search_requested.emit(suggestion.text, True)

    # -- handlers
    def _on_text_edited(self, text: str) -> None:
        t0 = time.perf_counter()
        self._selection_moved = False
        self._dismissed_for = None
        self.keystroke.emit()
        if self.field.preedit_active:
            return
        self.timer.start()
        self.last_handler_ms = (time.perf_counter() - t0) * 1000

    def _on_text_changed(self, text: str) -> None:
        if not text:
            self.hide_popup()

    def _fire_debounced(self) -> None:
        if self.field.preedit_active:
            return
        self.search_requested.emit(self.field.text(), False)

    def _on_enter(self) -> None:
        if self.popup.isVisible() and self.popup.accept_highlighted():
            return
        self.hide_popup()
        if self.is_pending() or self.field.text() != self.results_text:
            self.flush()
        else:
            self.open_requested.emit()

    def _on_tab(self) -> bool:
        return self.popup.isVisible() and self.popup.accept_highlighted()

    def _on_escape(self) -> None:
        if self.popup.isVisible():
            self._dismissed_for = self.field.text()
            self.hide_popup()
            return
        if self.field.text():
            self.timer.stop()
            self.field.clear()
            self.cleared.emit()
            return
        self.escape_to_hide.emit()

    def _on_nav(self, event: QKeyEvent) -> None:
        key = event.key()
        if self.popup.isVisible() and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.popup.move_highlight(1 if key == Qt.Key.Key_Down else -1)
            return
        self.hide_popup()
        self.nav_key.emit(event)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.field and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            if self.popup.isVisible():
                self.popup.place(self.field)
        return False
