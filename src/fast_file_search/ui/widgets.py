"""Shared widgets and dialogs: glyph icons, ElidedLabel, EmptyState, NoticeBar,
SettingsDialog, AboutDialog and export.

Nothing here touches SQLite or the file system on the GUI thread beyond what a
dialog needs (choosing a folder, writing an export the user asked for).
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QIcon, QIconEngine, QPainter, QPainterPath, QPalette, QPen, QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QSystemTrayIcon, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import kinds  # noqa: F401  (re-exported for the other ui modules)
from ..utils.platform_utils import paths_equal
from . import theme
from .theme import SPACE_L, SPACE_M, SPACE_S, SPACE_XL, SPACE_XS, SecondaryLabel


# --------------------------------------------------------------------------- resources
def find_resource(name: str) -> Optional[Path]:
    """Locate a bundled data file (e.g. ``files.ico``) in source, wheel or Nuitka builds.

    Looks next to the executable (Nuitka standalone/onefile data files), next to this
    module and its parents (source checkout), the PyInstaller bundle dir and the CWD.
    """
    roots: List[Path] = []
    compiled = globals().get("__compiled__")
    containing = getattr(compiled, "containing_dir", None)
    if containing:
        roots.append(Path(str(containing)))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    try:
        roots.extend(Path(__file__).resolve().parents)
    except (NameError, OSError):
        pass
    for exe in (sys.executable, sys.argv[0] if sys.argv else ""):
        if exe:
            try:
                roots.append(Path(exe).resolve().parent)
            except OSError:
                pass
    roots.append(Path.cwd())

    seen = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        for candidate in (root / name, root / "assets" / name):
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def app_icon() -> QIcon:
    """The application icon, or an empty QIcon when the resource is not bundled."""
    path = find_resource("files.ico")
    return QIcon(str(path)) if path else QIcon()


def app_version() -> str:
    # Compiled builds carry no package metadata, so use the in-package constant.
    from .. import __version__

    return __version__


# --------------------------------------------------------------------------- glyph icons
def _draw_glyph(p: QPainter, name: str, s: float) -> None:
    """Draw a 16-unit line glyph scaled to ``s`` px. The pen colour is already set."""
    k = s / 16.0

    def P(x: float, y: float) -> QPointF:
        return QPointF(x * k, y * k)

    pen = p.pen()
    if name == "search":
        p.drawEllipse(QRectF(2.2 * k, 2.2 * k, 8.4 * k, 8.4 * k))
        p.drawLine(P(9.4, 9.4), P(13.8, 13.8))
    elif name == "close":
        p.drawLine(P(4.5, 4.5), P(11.5, 11.5))
        p.drawLine(P(11.5, 4.5), P(4.5, 11.5))
    elif name == "clock":
        p.drawEllipse(QRectF(2 * k, 2 * k, 12 * k, 12 * k))
        p.drawLine(P(8, 8), P(8, 4.6))
        p.drawLine(P(8, 8), P(10.6, 9.6))
    elif name == "star":
        pts = []
        for i in range(10):
            r = 6.2 if i % 2 == 0 else 2.7
            a = -math.pi / 2 + i * math.pi / 5
            pts.append(P(8 + r * math.cos(a), 8.5 + r * math.sin(a)))
        p.drawPolygon(QPolygonF(pts))
    elif name == "image":
        p.drawRoundedRect(QRectF(2 * k, 3 * k, 12 * k, 10 * k), 1.5 * k, 1.5 * k)
        path = QPainterPath(P(2.5, 12))
        for x, y in ((6.2, 8), (8.8, 10.6), (10.4, 9), (13.5, 12)):
            path.lineTo(P(x, y))
        p.drawPath(path)
        p.drawEllipse(P(10.6, 6), 1.1 * k, 1.1 * k)
    elif name in ("document", "file"):
        path = QPainterPath(P(3.5, 1.5))
        for x, y in ((9.5, 1.5), (12.5, 4.5), (12.5, 14.5), (3.5, 14.5)):
            path.lineTo(P(x, y))
        path.closeSubpath()
        p.drawPath(path)
        p.drawPolyline(QPolygonF([P(9.5, 1.5), P(9.5, 4.5), P(12.5, 4.5)]))
        if name == "document":
            for y in (7.5, 9.8, 12.1):
                p.drawLine(P(5.6, y), P(10.4, y))
    elif name == "code":
        p.drawPolyline(QPolygonF([P(5.5, 4.5), P(2, 8), P(5.5, 11.5)]))
        p.drawPolyline(QPolygonF([P(10.5, 4.5), P(14, 8), P(10.5, 11.5)]))
        p.drawLine(P(9.2, 3.5), P(6.8, 12.5))
    elif name == "folder":
        path = QPainterPath(P(1.5, 4))
        for x, y in ((6, 4), (7.5, 5.5), (14.5, 5.5), (14.5, 13), (1.5, 13)):
            path.lineTo(P(x, y))
        path.closeSubpath()
        p.drawPath(path)
    elif name == "gear":
        p.drawEllipse(P(8, 8), 2.0 * k, 2.0 * k)
        path = QPainterPath()
        teeth = 8
        for i in range(teeth * 2):
            a0 = (i / (teeth * 2)) * 2 * math.pi
            a1 = ((i + 1) / (teeth * 2)) * 2 * math.pi
            r = 6.6 if i % 2 == 0 else 4.9
            if i == 0:
                path.moveTo(P(8 + r * math.cos(a0), 8 + r * math.sin(a0)))
            else:
                path.lineTo(P(8 + r * math.cos(a0), 8 + r * math.sin(a0)))
            path.lineTo(P(8 + r * math.cos(a1), 8 + r * math.sin(a1)))
        path.closeSubpath()
        p.drawPath(path)
    elif name == "warning":
        p.drawPolygon(QPolygonF([P(8, 2), P(14.5, 13.5), P(1.5, 13.5)]))
        p.drawLine(P(8, 6.3), P(8, 9.6))
        old = QPen(pen)
        dot = QPen(pen)
        dot.setWidthF(pen.widthF() * 1.5)
        p.setPen(dot)
        p.drawPoint(P(8, 11.7))
        p.setPen(old)
    elif name == "check":
        p.drawPolyline(QPolygonF([P(3.5, 8.5), P(6.8, 11.8), P(12.8, 4.6)]))
    elif name == "chevron":
        p.drawPolyline(QPolygonF([P(4.5, 6.5), P(8, 10), P(11.5, 6.5)]))
    elif name == "info":
        p.drawEllipse(QRectF(2 * k, 2 * k, 12 * k, 12 * k))
        p.drawLine(P(8, 7.2), P(8, 11.2))
        p.drawPoint(P(8, 5))


class _GlyphEngine(QIconEngine):
    """Monochrome line icon painted on demand with the current palette, so it is sharp
    at any scale and follows light/dark switches without being rebuilt."""

    def __init__(self, name: str, role: str = "secondary"):
        super().__init__()
        self.name = name
        self.role = role

    _colors: Dict[tuple, QColor] = {}

    def _color(self, mode: QIcon.Mode) -> QColor:
        pal = QApplication.palette()
        key = (self.role, mode.value, pal.cacheKey())
        c = self._colors.get(key)
        if c is None:
            if len(self._colors) > 64:
                self._colors.clear()
            c = self._colors[key] = self._resolve_color(pal, mode)
        return c

    def _resolve_color(self, pal: QPalette, mode: QIcon.Mode) -> QColor:
        if mode == QIcon.Mode.Disabled:
            c = QColor(pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText))
            return c
        if mode == QIcon.Mode.Selected:
            return QColor(pal.color(QPalette.ColorRole.HighlightedText))
        if self.role == "accent":
            return QColor(pal.color(QPalette.ColorRole.Highlight))
        if self.role == "text":
            return QColor(pal.color(QPalette.ColorRole.WindowText))
        if self.role == "error":
            return theme.error_text_color(pal)
        return theme.secondary_text_color(pal, pal.color(QPalette.ColorRole.Window))

    _pixmaps: Dict[tuple, QPixmap] = {}

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        # Painted through a small pixmap cache: views call this for every visible row.
        dev = painter.device()
        scale = dev.devicePixelRatioF() if dev is not None else 1.0
        side = min(rect.width(), rect.height())
        pm = self.scaledPixmap(QSize(side, side), mode, state, scale)
        painter.drawPixmap(rect.x() + (rect.width() - side) // 2, rect.y() + (rect.height() - side) // 2, pm)

    def _render(self, painter: QPainter, rect: QRect, mode: QIcon.Mode) -> None:
        side = min(rect.width(), rect.height())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2)
        pen = QPen(self._color(mode))
        pen.setWidthF(max(1.0, side / 12.0))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        _draw_glyph(painter, self.name, float(side))
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State,  # noqa: N802
                     scale: float) -> QPixmap:
        w = max(1, round(size.width() * scale))
        h = max(1, round(size.height() * scale))
        key = (self.name, self.role, w, h, scale, mode.value, self._color(mode).rgba())
        pm = self._pixmaps.get(key)
        if pm is None:
            if len(self._pixmaps) > 512:
                self._pixmaps.clear()
            pm = QPixmap(w, h)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            self._render(p, QRect(0, 0, w, h), mode)
            p.end()
            pm.setDevicePixelRatio(scale)
            self._pixmaps[key] = pm
        return pm

    def clone(self) -> QIconEngine:
        return _GlyphEngine(self.name, self.role)


_glyph_cache: Dict[Tuple[str, str], QIcon] = {}


def glyph_icon(name: str, role: str = "secondary") -> QIcon:
    """A palette-coloured line icon (search, close, clock, star, image, document,
    code, folder, file, gear, warning, info)."""
    key = (name, role)
    icon = _glyph_cache.get(key)
    if icon is None:
        icon = QIcon(_GlyphEngine(name, role))
        _glyph_cache[key] = icon
    return icon


# --------------------------------------------------------------------------- small widgets
class DropDownButton(QToolButton):
    """Tool button with an instant popup menu whose text never runs under the menu
    indicator: the native bevel and label are drawn by the style, the label in a
    rect that leaves room for a small chevron at the right."""

    ARROW = 12

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

    def sizeHint(self) -> QSize:  # noqa: N802
        base = super().sizeHint()
        fm = self.fontMetrics()
        w = fm.horizontalAdvance(self.text().replace("&&", "&")) + 2 * SPACE_S + self.ARROW + SPACE_XS
        return QSize(w, base.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, event):  # noqa: N802
        from PySide6.QtWidgets import QStyle, QStyleOptionToolButton, QStylePainter

        p = QStylePainter(self)
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        opt.features = opt.features & ~QStyleOptionToolButton.ToolButtonFeature.HasMenu
        opt.features = opt.features & ~QStyleOptionToolButton.ToolButtonFeature.MenuButtonPopup
        label = QStyleOptionToolButton(opt)
        opt.text = ""
        p.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, opt)
        r = self.rect()
        label.rect = QRect(r.left() + SPACE_S, r.top(), r.width() - 2 * SPACE_S - self.ARROW - SPACE_XS, r.height())
        if opt.state & QStyle.StateFlag.State_Sunken:
            shift = self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonShiftHorizontal, opt, self)
            label.rect.translate(shift, self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonShiftVertical, opt, self))
        p.drawControl(QStyle.ControlElement.CE_ToolButtonLabel, label)
        size = 10
        ar = QRect(r.right() - SPACE_S - size + 2, r.center().y() - size // 2 + 1, size, size)
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        glyph_icon("chevron", "text").paint(p, ar, Qt.AlignmentFlag.AlignCenter, mode)


class ElidedLabel(QLabel):
    """Single-line label that elides instead of forcing its parent wider."""

    def __init__(self, text: str = "", mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: Optional[str]) -> None:  # noqa: N802 (Qt naming)
        self._full = text or ""
        super().setText(self._full)
        self._update_tooltip()

    def text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def _elided(self) -> str:
        return self.fontMetrics().elidedText(self._full, self._mode, self.contentsRect().width())

    def _update_tooltip(self) -> None:
        self.setToolTip(self._full if self._elided() != self._full else "")

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._update_tooltip()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        self.style().drawItemText(
            painter, self.contentsRect(), int(self.alignment() | Qt.AlignmentFlag.AlignVCenter),
            self.palette(), self.isEnabled(), self._elided(), self.foregroundRole(),
        )


class EmptyState(QFrame):
    """Centered title, one-line explanation and optional buttons."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.kind = ""
        self.buttons: List[QPushButton] = []
        self.setObjectName("EmptyState")
        # Same frame and background as the results list it stands in for.
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)

        root = QHBoxLayout(self)
        root.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_XL)
        column = QWidget()
        column.setMaximumWidth(460)
        column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        root.addStretch(1)
        root.addWidget(column, 100)
        root.addStretch(1)
        outer = QVBoxLayout(column)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE_S)
        outer.addStretch(2)

        self.title = QLabel()
        self.title.setFont(theme.ui_font(1.25, QFont.Weight.DemiBold))
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.message = SecondaryLabel()
        self.message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for lbl in (self.title, self.message):
            outer.addWidget(lbl)
        self.link = QLabel()
        self.link.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.link.setTextFormat(Qt.TextFormat.RichText)
        self.link.setVisible(False)
        outer.addWidget(self.link)
        outer.addSpacing(SPACE_L)

        self.button_row = QHBoxLayout()
        self.button_row.setSpacing(SPACE_S)
        self.button_row.addStretch()
        self.button_row.addStretch()
        outer.addLayout(self.button_row)
        outer.addStretch(3)

    def set_content(self, kind: str, title: str, message: str = "",
                    actions: Sequence[Tuple[str, Callable[[], None]]] = (),
                    link: str = "") -> None:
        self.kind = kind
        self.title.setText(title)
        self.message.setText(message)
        self.message.setVisible(bool(message))
        self.link.setText(link)
        self.link.setVisible(bool(link))
        for b in self.buttons:
            # Hide first: until the deferred delete runs, a removed button would
            # otherwise be painted over the content at its default size.
            b.hide()
            self.button_row.removeWidget(b)
            b.deleteLater()
        self.buttons = []
        for i, (text, callback) in enumerate(actions):
            btn = QPushButton(text)
            btn.setAccessibleName(text.rstrip("…"))
            btn.clicked.connect(lambda _=False, cb=callback: cb())
            self.button_row.insertWidget(1 + i, btn)
            self.buttons.append(btn)


class NoticeBar(QFrame):
    """A one-line message above the results that needs the user's attention
    (permission denied, indexer restarting...). Hidden while there is nothing to say.

    Notices are keyed; the most recent one is shown. Dismissing a key hides it until
    it is posted again with ``force=True``.
    """
    dismissed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("NoticeBar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.AlternateBase)
        self.setAccessibleName("Notice")
        self._notices: Dict[str, Tuple[str, str, Sequence[Tuple[str, Callable[[], None]]], bool]] = {}
        self._order: List[str] = []
        self._dismissed: set = set()
        self.key = ""

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_M, SPACE_S, SPACE_S, SPACE_S)
        row.setSpacing(SPACE_S)
        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.text, 1)
        self.buttons_row = QHBoxLayout()
        self.buttons_row.setSpacing(SPACE_S)
        row.addLayout(self.buttons_row)
        self.buttons: List[QPushButton] = []
        self.dismiss_btn = QPushButton("Dismiss")
        self.dismiss_btn.setAccessibleName("Dismiss notice")
        self.dismiss_btn.clicked.connect(self.dismiss_current)
        row.addWidget(self.dismiss_btn)
        self.setVisible(False)

    def post(self, key: str, text: str, actions: Sequence[Tuple[str, Callable[[], None]]] = (),
             level: str = "warning", dismissible: bool = True, force: bool = False) -> None:
        if key in self._dismissed and not force:
            return
        self._dismissed.discard(key)
        self._notices[key] = (text, level, tuple(actions), dismissible)
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        self._render()

    def remove(self, key: str) -> None:
        if self._notices.pop(key, None) is not None:
            self._order.remove(key)
            self._render()

    def has(self, key: str) -> bool:
        return key in self._notices

    def dismiss_current(self) -> None:
        key = self.key
        if not key:
            return
        self._dismissed.add(key)
        self.remove(key)
        self.dismissed.emit(key)

    def _render(self) -> None:
        if not self._order:
            self.key = ""
            self.setVisible(False)
            return
        key = self._order[-1]
        text, level, actions, dismissible = self._notices[key]
        self.key = key
        self.text.setText(text)
        self.setAccessibleDescription(text)
        icon = glyph_icon("warning" if level != "info" else "info",
                          "error" if level == "error" else "text")
        self.icon.setPixmap(icon.pixmap(16, 16))
        for b in self.buttons:
            b.hide()
            self.buttons_row.removeWidget(b)
            b.deleteLater()
        self.buttons = []
        for label, cb in actions:
            b = QPushButton(label)
            b.setAccessibleName(label.rstrip("…"))
            b.clicked.connect(lambda _=False, cb=cb: cb())
            self.buttons_row.addWidget(b)
            self.buttons.append(b)
        self.dismiss_btn.setVisible(dismissible)
        self.setVisible(True)


class StatusDot(QWidget):
    """8 px circle in the status bar; its meaning is always repeated in the label."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.state = "loading"
        self.setFixedSize(SPACE_M, SPACE_M)
        self.setAccessibleName("Index status")

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.status_dot_color(self.state, self.palette()))
        d = 8.0
        p.drawEllipse(QRectF((self.width() - d) / 2, (self.height() - d) / 2, d, d))


class SectionLabel(QLabel):
    """Uppercase sidebar section heading ("LIBRARY")."""

    def __init__(self, text: str, parent: Optional[QWidget] = None):
        super().__init__(text.upper(), parent)
        self.setFont(theme.section_font())
        self.setAccessibleName(text.title())
        theme.make_secondary(self)
        self.setContentsMargins(SPACE_S, SPACE_S, 0, SPACE_XS)


# --------------------------------------------------------------------------- settings
def tray_wording() -> str:
    if theme.IS_MAC:
        return "Keep running in the menu bar when the window is closed"
    if theme.IS_WINDOWS:
        return "Keep running in the notification area when the window is closed"
    return "Keep running in the system tray when the window is closed"


_ROOT_STATE_TEXT = {
    "new": "Waiting to be indexed",
    "scanning": "Indexing…",
    "ok": "",
    "denied": "Access denied",
    "missing": "Folder not found",
    "error": "Could not be read",
}


def log_dir() -> Optional[Path]:
    """The GUI log folder from the platform module (P5), if it is available."""
    try:
        from ..utils import platform_utils

        getter = getattr(platform_utils, "get_log_dir", None)
        return Path(getter()) if getter is not None else None
    except Exception:
        return None


class SettingsDialog(QDialog):
    """Settings (Preferences on macOS): appearance, tray, indexed folders, excludes and
    the indexer switches of config v2. ``save_settings`` writes config.json through the
    config manager; the caller sends the new ``EngineConfig`` to the engine."""

    def __init__(self, config_manager, parent: Optional[QWidget] = None, *,
                 root_states: Optional[Mapping[str, Tuple[str, int]]] = None,
                 on_rebuild: Optional[Callable[[], None]] = None,
                 on_show_logs: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.setWindowTitle(theme.settings_label())
        self.setModal(True)
        self.resize(600, 520)
        self.indexing_changed = False
        self._root_states = dict(root_states or {})
        cfg = config_manager.config

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_L, SPACE_L, SPACE_L, SPACE_L)
        layout.setSpacing(SPACE_L)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Settings sections")
        layout.addWidget(self.tabs, 1)

        # -- General
        general = QWidget()
        form = QFormLayout(general)
        form.setContentsMargins(SPACE_L, SPACE_L, SPACE_L, SPACE_L)
        form.setHorizontalSpacing(SPACE_M)
        form.setVerticalSpacing(SPACE_M)
        self.appearance_combo = QComboBox()
        for text, mode in (("Use system setting", "system"), ("Light", "light"), ("Dark", "dark")):
            self.appearance_combo.addItem(text, mode)
        self.appearance_combo.setCurrentIndex(max(0, self.appearance_combo.findData(theme.current_appearance())))
        form.addRow("Appearance", self.appearance_combo)

        self.tray_check = QCheckBox(tray_wording())
        self.tray_check.setChecked(theme.setting_bool("close_to_tray", False))
        self.tray_check.setAccessibleName(tray_wording())
        if QSystemTrayIcon.isSystemTrayAvailable():
            form.addRow("", self.tray_check)
        else:
            self.tray_check.setVisible(False)
        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if label and field and isinstance(label.widget(), QLabel) and label.widget().text():
                label.widget().setBuddy(field.widget())
                field.widget().setAccessibleName(label.widget().text())
        self.tabs.addTab(general, "General")

        # -- Indexing
        indexing = QWidget()
        col = QVBoxLayout(indexing)
        col.setContentsMargins(SPACE_L, SPACE_L, SPACE_L, SPACE_L)
        col.setSpacing(SPACE_S)
        folders_lbl = QLabel("Indexed folders")
        folders_lbl.setFont(theme.ui_font(1.0, QFont.Weight.DemiBold))
        col.addWidget(folders_lbl)
        self.paths_list = QListWidget()
        self.paths_list.setAccessibleName("Indexed folders")
        self.paths_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for p in list(getattr(cfg, "index_paths", []) or []):
            self._add_path_item(p)
        folders_lbl.setBuddy(self.paths_list)
        col.addWidget(self.paths_list, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE_S)
        self.add_path_btn = QPushButton("Add folder…")
        self.add_path_btn.setAccessibleName("Add folder")
        self.add_path_btn.clicked.connect(self.add_indexed_path)
        self.remove_path_btn = QPushButton("Remove")
        self.remove_path_btn.setAccessibleName("Remove folder")
        self.remove_path_btn.clicked.connect(self.remove_indexed_path)
        self.remove_path_btn.setEnabled(False)
        self.paths_list.itemSelectionChanged.connect(
            lambda: self.remove_path_btn.setEnabled(bool(self.paths_list.selectedItems())))
        buttons.addWidget(self.add_path_btn)
        buttons.addWidget(self.remove_path_btn)
        buttons.addStretch()
        col.addLayout(buttons)

        col.addSpacing(SPACE_M)
        exclude_lbl = QLabel("Skip these names")
        exclude_lbl.setFont(theme.ui_font(1.0, QFont.Weight.DemiBold))
        col.addWidget(exclude_lbl)
        self.exclude_edit = QPlainTextEdit()
        self.exclude_edit.setAccessibleName("Skip these names")
        self.exclude_edit.setPlainText("\n".join(getattr(cfg, "exclude_patterns", []) or []))
        self.exclude_edit.setTabChangesFocus(True)
        exclude_lbl.setBuddy(self.exclude_edit)
        col.addWidget(self.exclude_edit, 1)
        col.addWidget(SecondaryLabel("One name, pattern or folder/sub sequence per line, "
                                     "for example node_modules, *.tmp or build/output."))

        col.addSpacing(SPACE_M)
        self.checks: Dict[str, QCheckBox] = {}
        self._check_defaults: Dict[str, bool] = {}

        def check(key: str, text: str, default: bool, visible: bool = True) -> QCheckBox:
            box = QCheckBox(text)
            box.setAccessibleName(text)
            box.setChecked(bool(getattr(cfg, key, default)))
            box.setVisible(visible)
            if visible:
                col.addWidget(box)
            self.checks[key] = box
            self._check_defaults[key] = default
            return box

        check("include_home_library", "Include ~/Library", False, visible=theme.IS_MAC)
        check("index_hidden", "Index hidden files (they are ranked lower)", True)
        check("descend_packages", "Search inside apps and packages", False, visible=theme.IS_MAC)
        check("live_monitoring", "Live monitoring (update the index as files change)", True)
        check("pause_on_battery", "Pause background rescans when on battery", True)

        col.addSpacing(SPACE_S)
        tools = QHBoxLayout()
        tools.setSpacing(SPACE_S)
        self.logs_btn = QPushButton("Show log folder")
        self.logs_btn.setAccessibleName("Show log folder")
        self.logs_btn.clicked.connect(on_show_logs or (lambda: None))
        self.logs_btn.setVisible(on_show_logs is not None)
        self.rebuild_btn = QPushButton("Rebuild index…")
        self.rebuild_btn.setAccessibleName("Rebuild index")
        self.rebuild_btn.clicked.connect(on_rebuild or (lambda: None))
        self.rebuild_btn.setVisible(on_rebuild is not None)
        tools.addWidget(self.logs_btn)
        tools.addWidget(self.rebuild_btn)
        tools.addStretch()
        col.addLayout(tools)
        self.tabs.addTab(indexing, "Indexing")

        self.error_lbl = QLabel()
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setVisible(False)
        layout.addWidget(self.error_lbl)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.save_settings)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    @property
    def appearance(self) -> str:
        return self.appearance_combo.currentData() or "system"

    def _add_path_item(self, path: str) -> None:
        item = QListWidgetItem(path)
        item.setData(Qt.ItemDataRole.UserRole, path)
        state, entries = self._root_states.get(path, ("", -1))
        note = _ROOT_STATE_TEXT.get(state, "")
        if not note and entries >= 0:
            note = f"{theme.format_count(entries)} items"
        if note:
            item.setText(f"{path}  ({note})")
        item.setToolTip(path)
        self.paths_list.addItem(item)

    def paths(self) -> List[str]:
        return [self.paths_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.paths_list.count())]

    def add_indexed_path(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add folder to index")
        if folder:
            # Qt returns forward slashes on Windows; store native paths and compare
            # case/separator-insensitively so the same folder is never listed twice.
            folder = os.path.normpath(folder)
            if any(paths_equal(p, folder) for p in self.paths()):
                return
            self._add_path_item(folder)

    def remove_indexed_path(self) -> None:
        for s in self.paths_list.selectedItems():
            self.paths_list.takeItem(self.paths_list.row(s))

    def _show_error(self, text: str) -> None:
        theme.set_error_text(self.error_lbl, True)
        self.error_lbl.setText(text)
        self.error_lbl.setVisible(bool(text))

    def save_settings(self) -> None:
        cfg = self.config_manager.config
        paths = self.paths()
        excludes: List[str] = []
        for pat in self.exclude_edit.toPlainText().split("\n"):
            pat = pat.strip()
            if pat and pat not in excludes:
                excludes.append(pat)
        bad = [p for p in paths if not os.path.isabs(p)]
        if bad:
            self._show_error(f'"{bad[0]}" is not a full folder path.')
            return
        values = {"index_paths": paths, "exclude_patterns": excludes}
        for key, box in self.checks.items():
            values[key] = box.isChecked()
        old = {k: getattr(cfg, k, None) for k in values}

        def norm(key, value):
            if isinstance(values[key], list):
                return list(value or [])
            return bool(self._check_defaults[key] if value is None else value)

        self.indexing_changed = any(norm(k, old[k]) != values[k] for k in values)
        for k, v in values.items():
            setattr(cfg, k, v)
        try:
            self.config_manager.save()
        except Exception as e:
            for k, v in old.items():
                setattr(cfg, k, v)
            QMessageBox.warning(self, "Settings not saved", f"The settings could not be saved.\n\n{e}")
            return
        settings = theme.ui_settings()
        settings.setValue("appearance", self.appearance)
        settings.setValue("close_to_tray", self.tray_check.isChecked())
        self.accept()


class AboutDialog(QDialog):
    """Name, version, what it does, versions of the runtime, license."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {theme.APP_NAME}")
        self.setModal(True)

        from PySide6 import __version__ as PYSIDE_VERSION_STR
        from PySide6.QtCore import qVersion

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_L)
        layout.setSpacing(SPACE_L)
        icon = app_icon()
        if not icon.isNull():
            icon_lbl = QLabel()
            icon_lbl.setPixmap(icon.pixmap(48, 48))
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
            layout.addWidget(icon_lbl, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(SPACE_XS)
        title = QLabel(theme.APP_NAME)
        title.setFont(theme.ui_font(1.3, QFont.Weight.DemiBold))
        col.addWidget(title)
        self.version_lbl = SecondaryLabel(f"Version {app_version()}")
        self.version_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.version_lbl)
        col.addSpacing(SPACE_M)
        desc = QLabel("Finds files and folders by name in the folders you choose. "
                      "The index is stored on this computer only.")
        desc.setWordWrap(True)
        col.addWidget(desc)
        col.addSpacing(SPACE_M)
        py = ".".join(str(v) for v in sys.version_info[:3])
        runtime = SecondaryLabel(f"Qt {qVersion()}, PySide {PYSIDE_VERSION_STR}, Python {py}\nMIT License")
        runtime.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(runtime)
        col.addSpacing(SPACE_L)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        col.addWidget(box)
        layout.addLayout(col, 1)
        self.setFixedWidth(440)


# --------------------------------------------------------------------------- export
def export_results(parent: Optional[QWidget], results: List[dict]) -> Optional[str]:
    """Ask for a file and write the results as CSV or JSON.

    Returns a status message for the status bar, or None when cancelled.  Errors are
    shown in a dialog.
    """
    if not results:
        return "There are no results to export."
    path, chosen = QFileDialog.getSaveFileName(
        parent, "Export results", "search-results.csv", "CSV (*.csv);;JSON (*.json)")
    if not path:
        return None
    as_json = path.lower().endswith(".json") or (chosen.startswith("JSON") and not path.lower().endswith(".csv"))
    if as_json and not path.lower().endswith(".json"):
        path += ".json"
    try:
        if as_json:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
        else:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Name", "Folder", "Size", "Modified", "Path", "Kind"])
                for item in results:
                    writer.writerow([item.get("name", ""), item.get("folder", ""),
                                     item.get("size", 0), item.get("modified", 0),
                                     item.get("path", ""), item.get("kind", "")])
    except OSError as e:
        QMessageBox.warning(parent, "Export failed",
                            f'Could not write "{Path(path).name}": {e.strerror or e}')
        return None
    n = len(results)
    return f'Exported {n:,} {"result" if n == 1 else "results"} to "{Path(path).name}".'
