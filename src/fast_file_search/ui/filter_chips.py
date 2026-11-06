"""Filter chips (design F.2): "Type: All", "Size: Any", "Modified: Any time",
"Path: All folders", "Exclude: None" and, while it is on, "Favorites".

Each chip is a pill painted from the palette around a ``QToolButton`` with an instant
popup menu; while its filter is active the pill gets a quiet accent tint and a clear
button inside it. Every user change emits
``filters_changed`` exactly once with the full ``FilterState``. Text editors inside
the menus apply on Enter or when the menu closes, never per keystroke.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QDate, QEvent, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette, QPen,
)
from PySide6.QtWidgets import (
    QDateEdit, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
    QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)

from ..engine.protocol import FilterState
from ..utils.platform_utils import is_path_under, paths_equal
from . import theme
from .theme import SPACE_S, SPACE_XS, SPACE_M
from .widgets import DropDownButton, glyph_icon

KB = 1024
MB = 1024 ** 2
GB = 1024 ** 3

TYPE_PRESETS: Tuple[Tuple[str, str], ...] = (
    ("all", "All"), ("folder", "Folders"), ("file", "Files"),
    ("image", "Images"), ("document", "Documents"), ("code", "Code"),
    ("audio", "Audio"), ("video", "Video"), ("archive", "Archives"),
)
SMART_KINDS = ("image", "document", "code")

SIZE_PRESETS: Tuple[Tuple[str, str, Optional[int], Optional[int]], ...] = (
    ("any", "Any", None, None),
    ("tiny", "Under 10 KB", None, 10 * KB),
    ("small", "10 KB – 1 MB", 10 * KB, MB),
    ("medium", "1 – 100 MB", MB, 100 * MB),
    ("large", "100 MB – 1 GB", 100 * MB, GB),
    ("huge", "Over 1 GB", GB, None),
)

DATE_PRESETS: Tuple[Tuple[str, str], ...] = (
    ("any", "Any time"), ("today", "Today"), ("yesterday", "Yesterday"),
    ("week", "Past 7 days"), ("month", "Past 30 days"), ("year", "Past year"),
)

_SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(b|bytes?|k|kb|kib|m|mb|mib|g|gb|gib|t|tb|tib)?\s*$", re.I)
_UNITS = {"": 1, "b": 1, "byte": 1, "bytes": 1, "k": KB, "kb": KB, "kib": KB, "m": MB, "mb": MB,
          "mib": MB, "g": GB, "gb": GB, "gib": GB, "t": 1024 * GB, "tb": 1024 * GB, "tib": 1024 * GB}


def parse_size(text: str) -> Optional[int]:
    """``"10 kb"`` → 10240, ``"2.5 MB"`` → 2621440; ``None`` when empty; raises ValueError."""
    text = (text or "").strip()
    if not text:
        return None
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f'"{text}" is not a size. Use a number and a unit, for example 10 KB or 2.5 MB.')
    return int(float(m.group(1).replace(",", ".")) * _UNITS[(m.group(2) or "").lower()])


def split_list(text: str) -> List[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def _short_size(n: Optional[int]) -> str:
    return theme.format_size(n) if n is not None else ""


# --------------------------------------------------------------------------- chip state
@dataclass(frozen=True)
class ChipState:
    """Symbolic chip values. ``to_filter_state`` resolves relative dates."""
    type_key: str = "all"
    exts: Tuple[str, ...] = ()
    size_key: str = "any"
    size_min: Optional[int] = None          # custom only
    size_max: Optional[int] = None
    date_key: str = "any"
    date_from: Optional[str] = None         # ISO dates, custom only (inclusive)
    date_to: Optional[str] = None
    root_id: Optional[int] = None
    folder: Optional[str] = None
    path_contains: Optional[str] = None
    excludes: Tuple[str, ...] = ()
    favorites: bool = False

    def is_default(self) -> bool:
        return self == ChipState()

    def to_filter_state(self, now: Optional[float] = None) -> FilterState:
        kinds: frozenset = frozenset()
        exts: frozenset = frozenset()
        if self.type_key == "exts":
            exts = frozenset(e.lower().lstrip(".") for e in self.exts if e.strip("."))
        elif self.type_key != "all":
            kinds = frozenset({self.type_key})
        smin, smax = None, None
        if self.size_key == "custom":
            smin, smax = self.size_min, self.size_max
        else:
            for key, _label, lo, hi in SIZE_PRESETS:
                if key == self.size_key:
                    smin, smax = lo, hi
        mmin, mmax = self._date_range(now)
        return FilterState(
            kinds=kinds, exts=exts, size_min=smin, size_max=smax, mtime_min=mmin, mtime_max=mmax,
            root_ids=frozenset({self.root_id}) if self.root_id is not None else frozenset(),
            folder=self.folder, path_contains=self.path_contains or None,
            excludes=tuple(self.excludes), favorites_only=self.favorites,
        )

    def _date_range(self, now: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
        key = self.date_key
        if key == "any":
            return None, None
        # Rounded to the minute so repeated requests hit the engine's mask cache.
        now = float(int((time.time() if now is None else now) // 60) * 60)
        today = QDate.currentDate()
        midnight = float(today.startOfDay().toSecsSinceEpoch())
        day = 86400.0
        if key == "today":
            return midnight, None
        if key == "yesterday":
            return float(today.addDays(-1).startOfDay().toSecsSinceEpoch()), midnight
        if key == "week":
            return now - 7 * day, None
        if key == "month":
            return now - 30 * day, None
        if key == "year":
            return now - 365 * day, None
        if key == "custom":
            lo = QDate.fromString(self.date_from or "", Qt.DateFormat.ISODate)
            hi = QDate.fromString(self.date_to or "", Qt.DateFormat.ISODate)
            if lo.isValid() and hi.isValid() and lo > hi:
                lo, hi = hi, lo
            return (float(lo.startOfDay().toSecsSinceEpoch()) if lo.isValid() else None,
                    float(hi.addDays(1).startOfDay().toSecsSinceEpoch()) if hi.isValid() else None)
        return None, None


# --------------------------------------------------------------------------- chip widget
CHIP_RADIUS = 6
CHEVRON = 10


def chip_height(widget: QWidget) -> int:
    """One height for every chip and the "Clear filters" button: roughly a line edit's."""
    return max(28, widget.fontMetrics().height() + 2 * SPACE_S)


def chip_colors(palette: QPalette, active: bool, hover: bool) -> Tuple[QColor, QColor]:
    """(fill, border) for a chip. Active chips get a quiet accent tint, not a solid fill,
    so the label keeps full text contrast in light and dark palettes."""
    window = palette.color(QPalette.ColorRole.Window)
    text = palette.color(QPalette.ColorRole.Text)
    if active:
        accent = palette.color(QPalette.ColorRole.Highlight)
        fill = theme._mix(accent, window, 0.26 if theme.is_dark(palette) else 0.14)
        return fill, accent
    fill = palette.color(QPalette.ColorRole.Button)
    if hover:
        fill = theme._mix(text, fill, 0.08)
    return fill, theme._mix(text, window, 0.24)


class _ChipButton(DropDownButton):
    """The label part of a chip: "Name: value" plus a chevron, with no bevel of its own
    (the parent ``Chip`` paints the pill). Checked state is set by the bar only; clicking
    opens the menu, never toggles."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def nextCheckState(self) -> None:  # noqa: N802
        pass

    def _parts(self) -> Tuple[str, str]:
        name, sep, value = self.text().replace("&&", "&").partition(": ")
        return (name + sep, value) if sep else ("", name)

    def _value_font(self) -> QFont:
        f = QFont(self.font())
        if self.isChecked():
            f.setWeight(QFont.Weight.DemiBold)
        return f

    def sizeHint(self) -> QSize:  # noqa: N802
        head, value = self._parts()
        w = (SPACE_M + self.fontMetrics().horizontalAdvance(head)
             + QFontMetrics(self._value_font()).horizontalAdvance(value))
        w += SPACE_S + CHEVRON + SPACE_S if self.ARROW > 0 else SPACE_M
        return QSize(w, chip_height(self))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        fill, _ = chip_colors(pal, self.isChecked(), False)
        head, value = self._parts()
        r = self.rect()
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        x = SPACE_M
        if head:
            p.setFont(self.font())
            p.setPen(theme.secondary_text_color(pal, fill))
            p.drawText(QRect(x, r.top(), r.width() - x, r.height()), align, head)
            x += self.fontMetrics().horizontalAdvance(head)
        p.setFont(self._value_font())
        role = QPalette.ColorRole.Text if self.isEnabled() else QPalette.ColorRole.PlaceholderText
        p.setPen(pal.color(role))
        p.drawText(QRect(x, r.top(), r.width() - x, r.height()), align, value)
        if self.ARROW > 0:
            ar = QRect(r.right() - SPACE_S - CHEVRON + 1, r.center().y() - CHEVRON // 2 + 1, CHEVRON, CHEVRON)
            mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
            glyph_icon("chevron", "text").paint(p, ar, Qt.AlignmentFlag.AlignCenter, mode)


class Chip(QWidget):
    """One pill: the label button and, while the filter is active, a clear button inside it."""
    clear_clicked = Signal()

    def __init__(self, name: str, menu: Optional[QMenu], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.name = name
        self.setObjectName(f"Chip{name}")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(0)
        self.button = _ChipButton()
        self.button.setObjectName(f"Chip{name}Button")
        self.button.setCheckable(True)
        if menu is not None:
            self.button.setMenu(menu)
        else:
            self.button.ARROW = -SPACE_XS   # the Favorites chip has no menu: no chevron
            self.button.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
        self.row.addWidget(self.button)
        side = chip_height(self) - 2 * SPACE_XS
        self.clear_btn = QToolButton()
        self.clear_btn.setObjectName(f"Chip{name}Clear")
        self.clear_btn.setAutoRaise(True)
        self.clear_btn.setIcon(glyph_icon("close"))
        self.clear_btn.setIconSize(QSize(12, 12))
        self.clear_btn.setFixedSize(side, side)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setToolTip(f"Clear the {name.lower()} filter")
        self.clear_btn.setAccessibleName(f"Clear {name} filter")
        self.clear_btn.clicked.connect(self.clear_clicked)
        self.clear_btn.setVisible(False)
        self.row.addWidget(self.clear_btn)
        self.setFixedHeight(chip_height(self))
        for w in (self.button, self.clear_btn):
            w.installEventFilter(self)
        self.value = ""
        self.active = False

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave,
                            QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return False

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        hover = self.underMouse() or self.button.isDown()
        fill, border = chip_colors(pal, self.active, hover)
        width = 1.0
        if self.button.hasFocus():
            border, width = pal.color(QPalette.ColorRole.Highlight), 2.0
        inset = width / 2
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        p.setPen(QPen(border, width))
        p.setBrush(fill)
        p.drawRoundedRect(rect, CHIP_RADIUS, CHIP_RADIUS)

    def set_value(self, value: str, active: bool, tooltip: str = "") -> None:
        self.value = value
        self.active = active
        label = theme.chip_text(self.name, value)
        self.button.setText(label.replace("&", "&&"))
        self.button.setChecked(active)
        self.button.setToolTip(tooltip or f"{self.name}: {value}")
        self.button.setAccessibleName(f"{self.name} filter, {value}")
        self.clear_btn.setVisible(active)
        # The clear button sits inside the pill; keep a little room after it.
        self.row.setContentsMargins(0, 0, SPACE_XS if active else 0, 0)
        self.button.updateGeometry()
        self.update()

    def label(self) -> str:
        return f"{self.name}: {self.value}"


def _editor(placeholder: str, name: str, menu: QMenu) -> Tuple[QWidgetAction, QLineEdit, QLabel, QWidget]:
    """A QLineEdit (with an inline error label) for a menu."""
    box = QWidget()
    col = QVBoxLayout(box)
    col.setContentsMargins(SPACE_M, SPACE_XS, SPACE_M, SPACE_XS)
    col.setSpacing(SPACE_XS)
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    edit.setAccessibleName(name)
    edit.setClearButtonEnabled(True)
    edit.setMinimumWidth(220)
    col.addWidget(edit)
    err = QLabel()
    err.setWordWrap(True)
    err.setVisible(False)
    col.addWidget(err)
    act = QWidgetAction(menu)     # owned by the menu; the action owns the widget
    act.setDefaultWidget(box)
    return act, edit, err, box


def _set_error(label: QLabel, text: str) -> None:
    if text:
        theme.set_error_text(label, True)
    label.setText(text)
    label.setVisible(bool(text))


# --------------------------------------------------------------------------- bar
class FilterChipBar(QWidget):
    """The chip row. ``state()`` is the ``FilterState`` for the next request."""
    filters_changed = Signal(object)        # FilterState
    add_root_requested = Signal(str)        # "Choose folder…" outside the indexed folders

    RECENT_KEY = "filters/recent_excludes"

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("FilterChipBar")
        self._s = ChipState()
        self._roots: List[Tuple[int, str, str]] = []    # (id, display name, path)
        self.emit_count = 0

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_S)

        self.type_menu = self._build_type_menu()
        self.size_menu = self._build_size_menu()
        self.date_menu = self._build_date_menu()
        self.path_menu = QMenu(self)
        self.path_menu.aboutToShow.connect(self._fill_path_menu)
        self.path_menu.aboutToHide.connect(self._apply_path_contains)
        self.exclude_menu = self._build_exclude_menu()

        self.type_chip = Chip("Type", self.type_menu)
        self.size_chip = Chip("Size", self.size_menu)
        self.date_chip = Chip("Modified", self.date_menu)
        self.path_chip = Chip("Path", self.path_menu)
        self.exclude_chip = Chip("Exclude", self.exclude_menu)
        self.fav_chip = Chip("Favorites", None)
        self.chips = [self.type_chip, self.size_chip, self.date_chip, self.path_chip,
                      self.exclude_chip, self.fav_chip]
        self.type_chip.clear_clicked.connect(lambda: self._update(type_key="all", exts=()))
        self.size_chip.clear_clicked.connect(lambda: self._update(size_key="any", size_min=None, size_max=None))
        self.date_chip.clear_clicked.connect(lambda: self._update(date_key="any", date_from=None, date_to=None))
        self.path_chip.clear_clicked.connect(lambda: self._update(root_id=None, folder=None, path_contains=None))
        self.exclude_chip.clear_clicked.connect(lambda: self._update(excludes=()))
        self.fav_chip.clear_clicked.connect(lambda: self._update(favorites=False))
        for chip in self.chips:
            row.addWidget(chip)
        row.addStretch(1)
        self.clear_btn = QToolButton()
        self.clear_btn.setObjectName("ClearFilters")
        self.clear_btn.setText("Clear filters")
        self.clear_btn.setAutoRaise(True)
        self.clear_btn.setFixedHeight(chip_height(self))
        self.clear_btn.setAccessibleName("Clear filters")
        self.clear_btn.clicked.connect(self.clear)
        row.addWidget(self.clear_btn)
        self._render()

    # ------------------------------------------------------------------ menus
    def _build_type_menu(self) -> QMenu:
        menu = QMenu(self)
        self.type_group = QActionGroup(menu)
        self.type_actions: Dict[str, QAction] = {}
        for i, (key, label) in enumerate(TYPE_PRESETS):
            if key in ("image",):
                menu.addSeparator()
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setData(key)
            self.type_group.addAction(act)
            act.triggered.connect(lambda _=False, k=key: self._update(type_key=k, exts=()))
            self.type_actions[key] = act
        menu.addSeparator()
        act, self.ext_edit, self.ext_error, _ = _editor("pdf, docx", "Extensions", menu)
        menu.addAction(act)
        self.ext_edit.returnPressed.connect(lambda: (self._apply_exts(), menu.close()))
        menu.aboutToHide.connect(self._apply_exts)
        menu.aboutToShow.connect(lambda: self.ext_edit.setText(", ".join(self._s.exts)
                                                               if self._s.type_key == "exts" else ""))
        return menu

    def _apply_exts(self) -> None:
        exts = tuple(e.lower().lstrip(".") for e in split_list(self.ext_edit.text()) if e.strip("."))
        current = self._s.exts if self._s.type_key == "exts" else ()
        if exts == current:
            return
        if exts:
            self._update(type_key="exts", exts=exts)
        elif self._s.type_key == "exts":
            self._update(type_key="all", exts=())

    def _build_size_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        self.size_group = QActionGroup(menu)
        self.size_actions: Dict[str, QAction] = {}
        for key, label, _lo, _hi in SIZE_PRESETS:
            act = menu.addAction(label)
            act.setCheckable(True)
            self.size_group.addAction(act)
            act.triggered.connect(lambda _=False, k=key: self._update(size_key=k, size_min=None, size_max=None))
            self.size_actions[key] = act
        menu.addSeparator()
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(SPACE_M, SPACE_XS, SPACE_M, SPACE_XS)
        col.setSpacing(SPACE_XS)
        col.addWidget(QLabel("Custom (files only)"))
        line = QHBoxLayout()
        line.setSpacing(SPACE_S)
        self.size_min_edit = QLineEdit()
        self.size_min_edit.setPlaceholderText("Min, e.g. 10 KB")
        self.size_min_edit.setAccessibleName("Minimum size")
        self.size_max_edit = QLineEdit()
        self.size_max_edit.setPlaceholderText("Max, e.g. 2.5 MB")
        self.size_max_edit.setAccessibleName("Maximum size")
        line.addWidget(self.size_min_edit)
        line.addWidget(QLabel("to"))
        line.addWidget(self.size_max_edit)
        col.addLayout(line)
        self.size_error = QLabel()
        self.size_error.setWordWrap(True)
        self.size_error.setVisible(False)
        col.addWidget(self.size_error)
        apply = QPushButton("Apply")
        apply.setAccessibleName("Apply size")
        apply.setAutoDefault(False)
        col.addWidget(apply, 0, Qt.AlignmentFlag.AlignRight)
        act = QWidgetAction(menu)
        act.setDefaultWidget(box)
        menu.addAction(act)

        def go() -> None:
            if self._apply_custom_size():
                menu.close()

        apply.clicked.connect(go)
        self.size_min_edit.returnPressed.connect(go)
        self.size_max_edit.returnPressed.connect(go)
        return menu

    def _apply_custom_size(self) -> bool:
        try:
            lo = parse_size(self.size_min_edit.text())
            hi = parse_size(self.size_max_edit.text())
        except ValueError as exc:
            _set_error(self.size_error, str(exc))
            return False
        if lo is not None and hi is not None and lo >= hi:
            _set_error(self.size_error, "The minimum must be smaller than the maximum.")
            return False
        _set_error(self.size_error, "")
        if lo is None and hi is None:
            self._update(size_key="any", size_min=None, size_max=None)
        else:
            self._update(size_key="custom", size_min=lo, size_max=hi)
        return True

    def _build_date_menu(self) -> QMenu:
        menu = QMenu(self)
        self.date_group = QActionGroup(menu)
        self.date_actions: Dict[str, QAction] = {}
        for key, label in DATE_PRESETS:
            act = menu.addAction(label)
            act.setCheckable(True)
            self.date_group.addAction(act)
            act.triggered.connect(lambda _=False, k=key: self._update(date_key=k, date_from=None, date_to=None))
            self.date_actions[key] = act
        menu.addSeparator()
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(SPACE_M, SPACE_XS, SPACE_M, SPACE_XS)
        col.setSpacing(SPACE_XS)
        col.addWidget(QLabel("Custom range"))
        line = QHBoxLayout()
        line.setSpacing(SPACE_S)
        self.date_from_edit = QDateEdit(QDate.currentDate().addMonths(-1))
        self.date_to_edit = QDateEdit(QDate.currentDate())
        for edit, name in ((self.date_from_edit, "From date"), (self.date_to_edit, "To date")):
            edit.setCalendarPopup(True)
            edit.setAccessibleName(name)
            edit.setDisplayFormat("yyyy-MM-dd")
        line.addWidget(self.date_from_edit)
        line.addWidget(QLabel("to"))
        line.addWidget(self.date_to_edit)
        col.addLayout(line)
        apply = QPushButton("Apply")
        apply.setAccessibleName("Apply dates")
        apply.setAutoDefault(False)
        col.addWidget(apply, 0, Qt.AlignmentFlag.AlignRight)
        act = QWidgetAction(menu)
        act.setDefaultWidget(box)
        menu.addAction(act)

        def go() -> None:
            self._update(date_key="custom",
                         date_from=self.date_from_edit.date().toString(Qt.DateFormat.ISODate),
                         date_to=self.date_to_edit.date().toString(Qt.DateFormat.ISODate))
            menu.close()

        apply.clicked.connect(go)
        return menu

    def _fill_path_menu(self) -> None:
        menu = self.path_menu
        menu.clear()
        old = getattr(self, "_path_group", None)
        if old is not None:
            old.deleteLater()
        group = self._path_group = QActionGroup(menu)
        act = menu.addAction("All folders")
        act.setCheckable(True)
        act.setChecked(self._s.root_id is None and self._s.folder is None)
        group.addAction(act)
        act.triggered.connect(lambda: self._update(root_id=None, folder=None))
        if self._roots:
            menu.addSeparator()
        for rid, name, path in self._roots:
            a = menu.addAction(name)
            a.setToolTip(path)
            a.setCheckable(True)
            a.setChecked(self._s.root_id == rid and self._s.folder is None)
            group.addAction(a)
            a.triggered.connect(lambda _=False, r=rid: self._update(root_id=r, folder=None))
        menu.addSeparator()
        choose = menu.addAction("Choose folder…")
        choose.triggered.connect(self.choose_folder)
        act, self.contains_edit, _err, _ = _editor("Path contains…", "Path contains", menu)
        self.contains_edit.setText(self._s.path_contains or "")
        self.contains_edit.returnPressed.connect(menu.close)
        menu.addAction(act)

    def _apply_path_contains(self) -> None:
        edit = getattr(self, "contains_edit", None)
        if edit is None:
            return
        try:
            text = edit.text().strip() or None
        except RuntimeError:  # the menu was rebuilt
            return
        if text != self._s.path_contains:
            self._update(path_contains=text)

    def choose_folder(self) -> None:
        start = self._s.folder or (self._roots[0][2] if self._roots else "")
        folder = QFileDialog.getExistingDirectory(self, "Search in folder", start)
        if not folder:
            return
        self.set_folder(os.path.normpath(folder), ask_to_add=True)

    def set_folder(self, folder: str, ask_to_add: bool = False) -> bool:
        """Scope to a folder inside an indexed folder ("Search in this folder")."""
        root = self.root_of(folder)
        if root is None:
            if ask_to_add:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Information)
                box.setWindowTitle("Not in an indexed folder")
                box.setText("Not in an indexed folder")
                box.setInformativeText(f'"{folder}" is not inside any indexed folder. Add it to the index?')
                add = box.addButton("Add folder", QMessageBox.ButtonRole.AcceptRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                if box.clickedButton() is add:
                    self.add_root_requested.emit(folder)
            return False
        rid, _name, rpath = root
        if paths_equal(folder, rpath):
            self._update(root_id=rid, folder=None)
        else:
            self._update(root_id=None, folder=folder)
        return True

    def _build_exclude_menu(self) -> QMenu:
        menu = QMenu(self)
        act, self.exclude_edit, _err, _ = _editor("node_modules, *.tmp", "Exclude", menu)
        menu.addAction(act)
        self.exclude_recent_sep = menu.addSeparator()
        self.exclude_edit.returnPressed.connect(menu.close)
        menu.aboutToShow.connect(self._fill_exclude_menu)
        menu.aboutToHide.connect(self._apply_excludes)
        return menu

    def recent_excludes(self) -> List[List[str]]:
        raw = theme.ui_settings().value(self.RECENT_KEY, [])
        if isinstance(raw, str):
            raw = [raw]
        return [split_list(x) for x in (raw or []) if isinstance(x, str) and split_list(x)][:5]

    def _remember_excludes(self, excludes: Sequence[str]) -> None:
        if not excludes:
            return
        text = ", ".join(excludes)
        items = [", ".join(x) for x in self.recent_excludes()]
        items = [text] + [x for x in items if x != text]
        theme.ui_settings().setValue(self.RECENT_KEY, items[:5])

    def _fill_exclude_menu(self) -> None:
        menu = self.exclude_menu
        for a in list(menu.actions()):
            if getattr(a, "_recent", False):
                menu.removeAction(a)
        self.exclude_edit.setText(", ".join(self._s.excludes))
        recent = self.recent_excludes()
        self.exclude_recent_sep.setVisible(bool(recent))
        for items in recent:
            a = QAction(", ".join(items), menu)
            a._recent = True  # type: ignore[attr-defined]
            a.triggered.connect(lambda _=False, it=tuple(items): self._set_excludes(it))
            menu.addAction(a)

    def _set_excludes(self, items: Tuple[str, ...]) -> None:
        self.exclude_edit.setText(", ".join(items))
        self._update(excludes=tuple(items))
        self._remember_excludes(items)

    def _apply_excludes(self) -> None:
        items = tuple(split_list(self.exclude_edit.text()))
        if items != self._s.excludes:
            self._update(excludes=items)
            self._remember_excludes(items)

    # ------------------------------------------------------------------ state
    def _update(self, **changes) -> None:
        new = replace(self._s, **changes)
        if new == self._s:
            self._render()
            return
        self._s = new
        self._render()
        self.emit_count += 1
        self.filters_changed.emit(self.state())

    def chip_state(self) -> ChipState:
        return self._s

    def state(self) -> FilterState:
        return self._s.to_filter_state()

    def set_chip_state(self, s: ChipState, emit: bool = False) -> None:
        if emit:
            self._update(**{f: getattr(s, f) for f in s.__dataclass_fields__})
        else:
            self._s = s
            self._render()

    def set_state(self, s: FilterState, emit: bool = False) -> None:
        """Show ``s`` in the chips (best effort for dates: they become a custom range)."""
        type_key, exts = "all", ()
        if s.exts:
            type_key, exts = "exts", tuple(sorted(s.exts))
        elif len(s.kinds) == 1:
            type_key = next(iter(s.kinds))
        size_key, smin, smax = "custom", s.size_min, s.size_max
        for key, _l, lo, hi in SIZE_PRESETS:
            if (lo, hi) == (s.size_min, s.size_max):
                size_key, smin, smax = key, None, None
        date_key, dfrom, dto = "any", None, None
        if s.mtime_min is not None or s.mtime_max is not None:
            date_key = "custom"
            if s.mtime_min is not None:
                dfrom = time.strftime("%Y-%m-%d", time.localtime(s.mtime_min))
            if s.mtime_max is not None:
                dto = time.strftime("%Y-%m-%d", time.localtime(s.mtime_max - 1))
        cs = ChipState(type_key=type_key, exts=exts, size_key=size_key, size_min=smin, size_max=smax,
                       date_key=date_key, date_from=dfrom, date_to=dto,
                       root_id=next(iter(s.root_ids)) if len(s.root_ids) == 1 else None,
                       folder=s.folder, path_contains=s.path_contains, excludes=tuple(s.excludes),
                       favorites=s.favorites_only)
        self.set_chip_state(cs, emit=emit)

    def clear(self) -> None:
        """Reset every chip (one request)."""
        self.set_chip_state(ChipState(), emit=True)

    def is_active(self) -> bool:
        return not self._s.is_default()

    def active_labels(self) -> List[str]:
        return [c.label() for c in self.chips if c.active]

    # -- sidebar / smart filter sync
    def set_roots(self, roots: Sequence[Tuple[int, str, str]]) -> None:
        self._roots = list(roots)
        ids = {r[0] for r in self._roots}
        if self._s.root_id is not None and self._s.root_id not in ids:
            self._update(root_id=None)
        else:
            self._render()

    def root_of(self, path: str) -> Optional[Tuple[int, str, str]]:
        best = None
        for r in self._roots:
            if paths_equal(path, r[2]) or is_path_under(path, r[2]):
                if best is None or len(r[2]) > len(best[2]):
                    best = r
        return best

    def root_id(self) -> Optional[int]:
        return self._s.root_id

    def scope_root_id(self) -> Optional[int]:
        """The root the scope lives in (a folder scope counts as its root)."""
        if self._s.root_id is not None:
            return self._s.root_id
        if self._s.folder:
            r = self.root_of(self._s.folder)
            return r[0] if r else None
        return None

    def set_root(self, root_id: Optional[int]) -> None:
        self._update(root_id=root_id, folder=None)

    def set_kind(self, kind: Optional[str]) -> None:
        self._update(type_key=kind or "all", exts=())

    def set_recent(self, on: bool) -> None:
        self._update(date_key="week" if on else "any", date_from=None, date_to=None)

    def set_favorites(self, on: bool) -> None:
        self._update(favorites=bool(on))

    def smart_state(self) -> Dict[str, bool]:
        s = self._s
        out = {k: s.type_key == k for k in SMART_KINDS}
        out["recent"] = s.date_key == "week"
        out["favorites"] = s.favorites
        return out

    # ------------------------------------------------------------------ render
    def _root_name(self, rid: Optional[int]) -> str:
        for r in self._roots:
            if r[0] == rid:
                return r[1]
        return "Folder"

    def _render(self) -> None:
        s = self._s
        labels = dict(TYPE_PRESETS)
        if s.type_key == "exts":
            self.type_chip.set_value(", ".join(s.exts), True)
        else:
            self.type_chip.set_value(labels.get(s.type_key, "All"), s.type_key != "all")
        for key, act in self.type_actions.items():
            act.setChecked(key == s.type_key)

        if s.size_key == "custom":
            lo, hi = _short_size(s.size_min), _short_size(s.size_max)
            value = f"{lo} – {hi}" if lo and hi else (f"Over {lo}" if lo else f"Under {hi}")
        else:
            value = next(label for key, label, _a, _b in SIZE_PRESETS if key == s.size_key)
        self.size_chip.set_value(value, s.size_key != "any", f"Size: {value}. Applies to files only.")
        for key, act in self.size_actions.items():
            act.setChecked(key == s.size_key)

        if s.date_key == "custom":
            value = f"{s.date_from or '…'} – {s.date_to or '…'}"
        else:
            value = dict(DATE_PRESETS).get(s.date_key, "Any time")
        self.date_chip.set_value(value, s.date_key != "any")
        for key, act in self.date_actions.items():
            act.setChecked(key == s.date_key)

        parts = []
        tip = ""
        if s.folder:
            parts.append(os.path.basename(s.folder.rstrip("\\/")) or s.folder)
            tip = s.folder
        elif s.root_id is not None:
            parts.append(self._root_name(s.root_id))
            tip = next((r[2] for r in self._roots if r[0] == s.root_id), "")
        if s.path_contains:
            parts.append(f"contains “{s.path_contains}”")
        active = bool(parts)
        value = ", ".join(parts) if parts else "All folders"
        self.path_chip.set_value(value, active, f"Path: {tip or value}")

        self.exclude_chip.set_value(", ".join(s.excludes) if s.excludes else "None", bool(s.excludes))
        self.fav_chip.set_value("On", s.favorites, "Only favorites")
        self.fav_chip.button.setText("Favorites")
        self.fav_chip.setVisible(s.favorites)
        self.clear_btn.setVisible(not s.is_default())
