"""Shared look-and-feel helpers for the UI.

Design rules used throughout the UI package:

* Spacing comes from one scale (``SPACE_*``: 4/8/12/16/24 px).
* Colours come from the platform palette.  The only accent is the palette's
  ``Highlight`` (the system accent colour on Windows/macOS); secondary text is a
  blend of ``Text`` and ``Base`` that stays above WCAG AA contrast.
* Fonts are the system UI / fixed fonts; hierarchy uses size and weight sparingly.
* No widget-wide style sheets: they break native styles (especially on macOS).
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
from typing import Optional

from PySide6.QtCore import QDateTime, QEvent, QLocale, QObject, QSettings, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QWidget

# --------------------------------------------------------------------------- spacing
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24

APP_NAME = "Fast File Search Pro"

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- settings
def ui_settings() -> QSettings:
    """Per-user UI state (window geometry, column widths, appearance...).

    Stored as an INI file in the platform's per-user config location, so tests and
    portable setups can redirect it with ``QSettings.setPath``.
    """
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, APP_NAME, "ui")


def setting_bool(key: str, default: bool) -> bool:
    value = ui_settings().value(key, default)
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes")
    return bool(value)


# --------------------------------------------------------------------------- wording
def reveal_label() -> str:
    """Platform wording for "select the file in the file manager"."""
    if IS_MAC:
        return "Show in Finder"
    if IS_WINDOWS:
        return "Show in File Explorer"
    return "Show in File Manager"


def trash_label() -> str:
    return "Move to Recycle Bin" if IS_WINDOWS else "Move to Trash"


def trash_name() -> str:
    return "the Recycle Bin" if IS_WINDOWS else "the Trash"


def settings_label() -> str:
    return "Preferences" if IS_MAC else "Settings"


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def display_text(text: str) -> str:
    """File names may contain newlines/tabs; show them as "?" (like ``ls -q``) so a
    name always stays on one line."""
    return _CONTROL.sub("?", text) if text else ""


# --------------------------------------------------------------------------- colours
def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """``t`` of colour ``a`` blended over ``(1 - t)`` of colour ``b``."""
    return QColor(
        round(a.red() * t + b.red() * (1 - t)),
        round(a.green() * t + b.green() * (1 - t)),
        round(a.blue() * t + b.blue() * (1 - t)),
    )


def _luminance(c: QColor) -> float:
    def ch(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def contrast_ratio(a: QColor, b: QColor) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def secondary_text_color(palette: Optional[QPalette] = None, background: Optional[QColor] = None) -> QColor:
    """Muted text colour that still meets WCAG AA (4.5:1) on the given background."""
    pal = palette or QApplication.palette()
    text = pal.color(QPalette.ColorRole.Text)
    bg = background or pal.color(QPalette.ColorRole.Base)
    t = 0.62
    color = _mix(text, bg, t)
    while contrast_ratio(color, bg) < 4.6 and t < 1.0:
        t = min(1.0, t + 0.04)
        color = _mix(text, bg, t)
    return color


def error_text_color(palette: Optional[QPalette] = None) -> QColor:
    return QColor("#ff99a4") if is_dark(palette) else QColor("#c42b1c")


def match_fill_color(palette: Optional[QPalette] = None) -> QColor:
    """Faint accent tint behind the matched part of a name (text stays full contrast)."""
    pal = palette or QApplication.palette()
    c = QColor(pal.color(QPalette.ColorRole.Highlight))
    c.setAlphaF(0.32 if is_dark(pal) else 0.20)
    return c


def is_dark(palette: Optional[QPalette] = None) -> bool:
    pal = palette or QApplication.palette()
    return pal.color(QPalette.ColorRole.Window).lightness() < 128


class _SecondaryText(QObject):
    """Event filter that keeps a widget's text in the secondary colour across
    light/dark switches (widgets get PaletteChange when the app palette changes)."""

    def __init__(self):
        super().__init__()
        self._busy = False

    def sync(self, widget: QWidget) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            app = QApplication.palette()
            color = secondary_text_color(app, app.color(QPalette.ColorRole.Window))
            pal = widget.palette()
            if pal.color(QPalette.ColorRole.WindowText) != color:
                pal.setColor(QPalette.ColorRole.WindowText, color)
                widget.setPalette(pal)
        finally:
            self._busy = False

    def eventFilter(self, obj, event):  # noqa: N802 (Qt naming)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.sync(obj)
        return False


_secondary_filter: Optional[_SecondaryText] = None


def make_secondary(widget: QWidget) -> QWidget:
    """Draw ``widget``'s text in the secondary colour (WCAG AA), following theme changes."""
    global _secondary_filter
    if _secondary_filter is None:
        _secondary_filter = _SecondaryText()
    widget.installEventFilter(_secondary_filter)
    _secondary_filter.sync(widget)
    return widget


def set_error_text(widget: QWidget, error: bool) -> None:
    """Switch a label between the secondary colour and the error colour."""
    if error:
        if _secondary_filter is not None:
            widget.removeEventFilter(_secondary_filter)
        pal = widget.palette()
        pal.setColor(QPalette.ColorRole.WindowText, error_text_color())
        widget.setPalette(pal)
    else:
        make_secondary(widget)


class SecondaryLabel(QLabel):
    """QLabel drawn in the secondary text colour; follows palette changes."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        make_secondary(self)


# --------------------------------------------------------------------------- fonts
def ui_font(scale: float = 1.0, weight: Optional[QFont.Weight] = None) -> QFont:
    font = QFont(QApplication.font())
    if scale != 1.0:
        size = font.pointSizeF()
        if size > 0:
            font.setPointSizeF(size * scale)
        elif font.pixelSize() > 0:
            font.setPixelSize(max(1, round(font.pixelSize() * scale)))
    if weight is not None:
        font.setWeight(weight)
    return font


def fixed_font() -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    base = QApplication.font().pointSizeF()
    if base > 0:
        font.setPointSizeF(base)
    return font


def tabular_font(font: Optional[QFont] = None) -> QFont:
    """Font with tabular (fixed-width) digits where the platform supports it."""
    f = QFont(font or QApplication.font())
    tag = getattr(QFont, "Tag", None)
    if tag is not None:
        try:
            f.setFeature(tag("tnum"), 1)
        except Exception:
            pass
    return f


# --------------------------------------------------------------------------- formatting
def format_count(n: int) -> str:
    return QLocale.system().toString(int(n))


def format_size(size: int) -> str:
    """Short human size: ``512 B``, ``4.2 KB``, ``482 KB``, ``1.3 GB``."""
    if size is None or size < 0:
        return ""
    if size < 1024:
        return f"{size} B"
    loc = QLocale.system()
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024 or unit == "TB":
            if value < 10:
                return f"{loc.toString(value, 'f', 1)} {unit}"
            return f"{loc.toString(round(value))} {unit}"
    return f"{size} B"  # pragma: no cover


def format_size_exact(size: int) -> str:
    if size < 1024:
        return f"{format_count(size)} bytes"
    return f"{format_size(size)} ({format_count(size)} bytes)"


def _to_qdatetime(ts: float) -> Optional[QDateTime]:
    try:
        return QDateTime.fromSecsSinceEpoch(int(ts))
    except (OverflowError, ValueError, TypeError):
        return None


def format_date_short(ts: float) -> str:
    """Precise, sortable ``2024-03-07 14:32`` (same width in every row)."""
    try:
        if not ts or float(ts) <= 0:
            return ""
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def format_date_full(ts: float) -> str:
    qdt = _to_qdatetime(ts) if ts else None
    if qdt is None or ts <= 0:
        return ""
    return QLocale.system().toString(qdt, QLocale.FormatType.LongFormat)


def format_timestamp_iso(ts: float) -> str:
    """Sortable, unambiguous ``YYYY-MM-DD HH:MM:SS`` (used in the details pane)."""
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return "Unknown"


# --------------------------------------------------------------------------- appearance
APPEARANCES = ("system", "light", "dark")
_original_palette: Optional[QPalette] = None


def _fallback_palette(dark: bool, accent: QColor) -> QPalette:
    """Neutral palette used only where the platform can't switch schemes itself."""
    R = QPalette.ColorRole
    G = QPalette.ColorGroup
    pal = QPalette()
    if dark:
        colors = {
            R.Window: "#202020", R.WindowText: "#f0f0f0", R.Base: "#1b1b1b",
            R.AlternateBase: "#242424", R.Text: "#f0f0f0", R.Button: "#2d2d2d",
            R.ButtonText: "#f0f0f0", R.BrightText: "#ffffff", R.Light: "#3c3c3c",
            R.Midlight: "#333333", R.Mid: "#4a4a4a", R.Dark: "#141414", R.Shadow: "#000000",
            R.ToolTipBase: "#2d2d2d", R.ToolTipText: "#f0f0f0", R.PlaceholderText: "#a8a8a8",
            R.Link: "#79b8ff", R.LinkVisited: "#b392f0", R.HighlightedText: "#ffffff",
        }
        disabled = "#7a7a7a"
    else:
        colors = {
            R.Window: "#f3f3f3", R.WindowText: "#1a1a1a", R.Base: "#ffffff",
            R.AlternateBase: "#f7f7f7", R.Text: "#1a1a1a", R.Button: "#fbfbfb",
            R.ButtonText: "#1a1a1a", R.BrightText: "#ffffff", R.Light: "#ffffff",
            R.Midlight: "#e5e5e5", R.Mid: "#c4c4c4", R.Dark: "#a0a0a0", R.Shadow: "#000000",
            R.ToolTipBase: "#ffffff", R.ToolTipText: "#1a1a1a", R.PlaceholderText: "#6b6b6b",
            R.Link: "#0062b8", R.LinkVisited: "#6f42c1", R.HighlightedText: "#ffffff",
        }
        disabled = "#9a9a9a"
    for role, value in colors.items():
        pal.setColor(role, QColor(value))
    pal.setColor(R.Highlight, accent)
    for role in (R.WindowText, R.Text, R.ButtonText):
        pal.setColor(G.Disabled, role, QColor(disabled))
    return pal


def current_appearance() -> str:
    value = str(ui_settings().value("appearance", "") or "")
    if value in APPEARANCES:
        return value
    # Migrate the old light/dark toggle: only an explicit "dark" choice is kept.
    try:
        from ..config import config_manager

        if config_manager.config.theme == "dark":
            return "dark"
    except Exception:
        pass
    return "system"


def apply_appearance(mode: str) -> None:
    """Follow the system scheme ("system") or force light/dark for this app."""
    global _original_palette
    app = QApplication.instance()
    if app is None or mode not in APPEARANCES:
        return
    hints = QGuiApplication.styleHints()
    wanted = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(mode, Qt.ColorScheme.Unknown)
    if hasattr(hints, "setColorScheme"):  # Qt >= 6.8
        try:
            hints.setColorScheme(wanted)
        except Exception:
            pass

    if _original_palette is None:
        _original_palette = QPalette(app.palette())

    if mode == "system":
        if hasattr(hints, "unsetColorScheme"):
            try:
                hints.unsetColorScheme()
            except Exception:
                pass
        if _palette_overridden:
            # An empty palette resolves to the platform palette again, so the app keeps
            # following later system scheme changes.
            app.setPalette(QPalette())
            _set_overridden(False)
        return

    want_dark = mode == "dark"
    if hints.colorScheme() == wanted and is_dark(app.palette()) == want_dark:
        if _palette_overridden:
            app.setPalette(QPalette())
            _set_overridden(False)
        return  # the platform switched the palette for us
    accent = QColor(_original_palette.color(QPalette.ColorRole.Highlight))
    if not accent.isValid() or accent.saturation() < 40:
        accent = QColor("#0067c0")
    app.setPalette(_fallback_palette(want_dark, accent))
    _set_overridden(True)


_palette_overridden = False


def _set_overridden(value: bool) -> None:
    global _palette_overridden
    _palette_overridden = value
