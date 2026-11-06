"""Search field, shortcut hint and Search button (design E.2).

There is no suggestion overlay: search-as-you-type updates the results list live and
that list is the suggestion UI. The bar never searches by itself: it emits ``search_requested(text, commit)`` after
the 120 ms debounce (``commit=False``) or on Enter / the Search button
(``commit=True``), and the main window turns that into one engine request.
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QKeyEvent, QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QStyle, QWidget

from . import theme
from .theme import SPACE_S, SPACE_XS
from .widgets import glyph_icon

DEBOUNCE_MS = 120

_NAV_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)


def shortcut_text(seq: str = "Ctrl+K") -> str:
    """``Ctrl+K`` on Windows/Linux, ``⌘K`` on macOS (Qt maps Ctrl to Cmd there)."""
    return QKeySequence(seq).toString(QKeySequence.SequenceFormat.NativeText)


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
    """The query field. Keys that belong to the results list are forwarded as signals
    so focus stays here."""
    nav_key = Signal(QKeyEvent)          # Up/Down/PageUp/PageDown/Ctrl+Home/End
    enter_pressed = Signal()
    escape_pressed = Signal()
    copy_requested = Signal()            # Ctrl+C with no text selected

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SearchField")
        self.setPlaceholderText("Search files and folders")
        self.setAccessibleName("Search")
        self.setAccessibleDescription(f"Type a file name. {shortcut_text('Ctrl+K')}")
        self.setClearButtonEnabled(True)
        self.setFont(theme.ui_font(1.1))
        self.preedit_active = False
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


# --------------------------------------------------------------------------- bar
class SearchBar(QWidget):
    """Search field + Search button. See the module docstring."""
    search_requested = Signal(str, bool)     # text, commit_history
    open_requested = Signal()                # Enter with nothing pending
    nav_key = Signal(QKeyEvent)
    escape_to_hide = Signal()                # Esc on an empty field
    cleared = Signal()
    copy_requested = Signal()
    keystroke = Signal()                     # any edit (the user typed)

    def __init__(self, parent: Optional[QWidget] = None):
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

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(DEBOUNCE_MS)
        self.timer.timeout.connect(self._fire_debounced)

        self.last_sent_text: Optional[str] = None   # text of the latest request
        self.results_text: Optional[str] = None     # text of the results on screen
        self.last_handler_ms = 0.0

        f = self.field
        f.textEdited.connect(self._on_text_edited)
        f.enter_pressed.connect(self._on_enter)
        f.escape_pressed.connect(self._on_escape)
        f.nav_key.connect(self._on_nav)
        f.copy_requested.connect(self.copy_requested)
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
        self.search_requested.emit(self.field.text(), True)

    def focus(self, select_all: bool = True) -> None:
        self.field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        if select_all:
            self.field.selectAll()

    # -- handlers
    def _on_text_edited(self, text: str) -> None:
        t0 = time.perf_counter()
        self.keystroke.emit()
        if self.field.preedit_active:
            return
        self.timer.start()
        self.last_handler_ms = (time.perf_counter() - t0) * 1000

    def _fire_debounced(self) -> None:
        if self.field.preedit_active:
            return
        self.search_requested.emit(self.field.text(), False)

    def _on_enter(self) -> None:
        if self.is_pending() or self.field.text() != self.results_text:
            self.flush()
        else:
            self.open_requested.emit()

    def _on_escape(self) -> None:
        if self.field.text():
            self.timer.stop()
            self.field.clear()
            self.cleared.emit()
            return
        self.escape_to_hide.emit()

    def _on_nav(self, event: QKeyEvent) -> None:
        self.nav_key.emit(event)
