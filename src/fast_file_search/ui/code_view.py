"""Read-only code view with a line-number gutter and optional syntax highlighting."""
from __future__ import annotations

import re
from typing import List, Optional

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QTextCursor, QTextFormat
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from . import theme
from .highlight import Highlighter, highlight_allowed

GUTTER_PAD = 8
MAX_MATCHES = 400


class _Gutter(QWidget):
    def __init__(self, view: "CodeView"):
        super().__init__(view)
        self._view = view
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._view.gutter_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        self._view.paint_gutter(self, event)


class CodeView(QPlainTextEdit):
    """Fixed-font, non-wrapping viewer.  Text is plain on purpose: rich text would
    render ``.html`` or ``.md`` sources instead of showing them."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._gutter = _Gutter(self)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setFont(theme.fixed_font())
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setAccessibleName("File preview")
        self.setUndoRedoEnabled(False)
        self._highlighter = Highlighter(self.document(), None)
        self._terms: List[str] = []
        self.painted_numbers: List[int] = []  # numbers drawn by the last gutter paint (tests)
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_gutter)
        self._update_margin()

    # ------------------------------------------------------------------ content
    @property
    def highlighter(self) -> Highlighter:
        return self._highlighter

    def set_content(self, text: str, language: Optional[str] = None) -> bool:
        """Show ``text``; highlight it when ``language`` is known and the text is not
        too large.  Returns whether highlighting is active."""
        hl = self._highlighter
        active = bool(language) and highlight_allowed(text)
        # Set the language first: replacing the text then highlights it exactly once,
        # synchronously, through the attached highlighter.
        hl.set_language(language if active else None, rehighlight=False)
        if hl.document() is None:
            hl.setDocument(self.document())
        self.setPlainText(text)
        active = active and hl.language is not None
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)
        self._apply_terms()
        return active

    def clear_content(self) -> None:
        self._highlighter.set_language(None, rehighlight=False)
        self.clear()
        self.setExtraSelections([])

    # ------------------------------------------------------------------ search terms
    def set_terms(self, terms: List[str]) -> None:
        self._terms = [t for t in terms if len(t) >= 2]
        self._apply_terms()

    def _apply_terms(self) -> None:
        if not self._terms or self.document().isEmpty():
            self.setExtraSelections([])
            return
        pattern = re.compile("|".join(re.escape(t) for t in self._terms), re.IGNORECASE)
        fill = theme.match_fill_color(self.palette())
        selections = []
        doc = self.document()
        for m in pattern.finditer(self.toPlainText()):
            cur = QTextCursor(doc)
            cur.setPosition(m.start())
            cur.setPosition(m.end(), QTextCursor.MoveMode.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cur
            sel.format.setBackground(fill)
            selections.append(sel)
            if len(selections) >= MAX_MATCHES:
                break
        self.setExtraSelections(selections)

    # ------------------------------------------------------------------ gutter
    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 2 * GUTTER_PAD + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self, *_args) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)
        self._place_gutter()

    def _place_gutter(self) -> None:
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def _update_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_gutter()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._highlighter.refresh_formats(self.palette())
            self._apply_terms()
            self._gutter.update()
        elif event.type() == QEvent.Type.FontChange:
            self._update_margin()

    def gutter_colors(self):
        pal = self.palette()
        base = pal.color(QPalette.ColorRole.Base)
        window = pal.color(QPalette.ColorRole.Window)
        background = theme._mix(window, base, 0.45)
        number = theme.secondary_text_color(pal, background)
        rule = theme._mix(pal.color(QPalette.ColorRole.Text), background, 0.14)
        return background, number, rule

    def paint_gutter(self, gutter: QWidget, event) -> None:
        background, number_color, rule = self.gutter_colors()
        painter = QPainter(gutter)
        painter.fillRect(event.rect(), background)
        painter.setPen(rule)
        painter.drawLine(gutter.width() - 1, event.rect().top(), gutter.width() - 1, event.rect().bottom())
        painter.setPen(number_color)
        painter.setFont(self.font())
        self.painted_numbers = []
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        height = self.fontMetrics().height()
        width = gutter.width() - GUTTER_PAD
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(0, top, width, height, Qt.AlignmentFlag.AlignRight, str(number + 1))
                self.painted_numbers.append(number + 1)
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1
        painter.end()
