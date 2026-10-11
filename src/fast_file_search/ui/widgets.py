"""Panels and dialogs: filters, preview, settings, about, export, empty states."""
from __future__ import annotations

import csv
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QDate, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor, QFont, QIcon, QImageReader, QPainter, QPalette, QPixmap, QSyntaxHighlighter,
    QTextCharFormat, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QSystemTrayIcon, QTabWidget,
    QVBoxLayout, QWidget,
)

from ..config import config_manager
from ..utils.file_utils import get_file_metadata
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


# --------------------------------------------------------------------------- small widgets
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
        self.message = SecondaryLabel()
        self.message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for lbl in (self.title, self.message):
            outer.addWidget(lbl)
        outer.addSpacing(SPACE_L)

        self.button_row = QHBoxLayout()
        self.button_row.setSpacing(SPACE_S)
        self.button_row.addStretch()
        self.button_row.addStretch()
        outer.addLayout(self.button_row)
        outer.addStretch(3)

    def set_content(self, kind: str, title: str, message: str = "",
                    actions: Sequence[Tuple[str, Callable[[], None]]] = ()) -> None:
        self.kind = kind
        self.title.setText(title)
        self.message.setText(message)
        self.message.setVisible(bool(message))
        for b in self.buttons:
            # Hide first: until the deferred delete runs, a removed button would
            # otherwise be painted over the content at its default size.
            b.hide()
            self.button_row.removeWidget(b)
            b.deleteLater()
        self.buttons = []
        for i, (text, callback) in enumerate(actions):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _=False, cb=callback: cb())
            self.button_row.insertWidget(1 + i, btn)
            self.buttons.append(btn)


# --------------------------------------------------------------------------- filters
_SIZE_PRESETS: List[Tuple[str, Optional[Tuple[Optional[int], Optional[int]]]]] = [
    ("Any size", None),
    ("Under 10 KB", (0, 10 * 1024)),
    ("10 KB to 1 MB", (10 * 1024, 1024 ** 2)),
    ("1 MB to 10 MB", (1024 ** 2, 10 * 1024 ** 2)),
    ("10 MB to 100 MB", (10 * 1024 ** 2, 100 * 1024 ** 2)),
    ("100 MB to 1 GB", (100 * 1024 ** 2, 1024 ** 3)),
    ("Over 1 GB", (1024 ** 3, None)),
]

_DATE_PRESETS = [
    ("Any time", "any"), ("Today", "today"), ("Yesterday", "yesterday"),
    ("Past 7 days", "week"), ("Past 30 days", "month"), ("Past year", "year"),
    ("Custom range", "custom"),
]


class FilterPanel(QFrame):
    """Type, size, date, path and exclude filters for the current search."""
    filters_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FilterPanel")
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, SPACE_S, 0, 0)
        layout.setSpacing(SPACE_S)

        # Typing in the regex/exclude boxes must not fire a full search per keystroke.
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(300)
        self._filter_timer.timeout.connect(self.filters_changed.emit)

        header = QHBoxLayout()
        header.setSpacing(SPACE_S)
        self.title_lbl = QLabel("Filters")
        self.title_lbl.setFont(theme.ui_font(1.0, QFont.Weight.DemiBold))
        header.addWidget(self.title_lbl)
        header.addStretch()
        self.clear_filters_btn = QPushButton("Reset")
        self.clear_filters_btn.setToolTip("Clear all filters")
        self.clear_filters_btn.clicked.connect(self.reset_filters)
        header.addWidget(self.clear_filters_btn)
        layout.addLayout(header)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(SPACE_M)
        form.setVerticalSpacing(SPACE_S)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form_layout = form

        self.ext_combo = QComboBox()
        self.ext_combo.addItem("All types", None)
        self.ext_combo.currentIndexChanged.connect(self._on_filter_changed)
        form.addRow("Type", self.ext_combo)

        self.size_combo = QComboBox()
        for text, rng in _SIZE_PRESETS:
            self.size_combo.addItem(text, rng)
        self.size_combo.currentIndexChanged.connect(self._on_filter_changed)
        form.addRow("Size", self.size_combo)

        self.date_combo = QComboBox()
        for text, key in _DATE_PRESETS:
            self.date_combo.addItem(text, key)
        self.date_combo.currentIndexChanged.connect(self._on_date_preset_changed)
        form.addRow("Modified", self.date_combo)

        self.custom_date_widget = QWidget()
        dates = QHBoxLayout(self.custom_date_widget)
        dates.setContentsMargins(0, 0, 0, 0)
        dates.setSpacing(SPACE_S)
        self.start_date_edit = QDateEdit(QDate.currentDate().addYears(-1))
        self.end_date_edit = QDateEdit(QDate.currentDate())
        for edit, name in ((self.start_date_edit, "From date"), (self.end_date_edit, "To date")):
            edit.setCalendarPopup(True)
            edit.setAccessibleName(name)
            edit.dateChanged.connect(self._on_filter_changed)
        dates.addWidget(self.start_date_edit, 1)
        dates.addWidget(QLabel("to"))
        dates.addWidget(self.end_date_edit, 1)
        self.custom_date_widget.setVisible(False)
        form.addRow("", self.custom_date_widget)

        self.regex_input = QLineEdit()
        self.regex_input.setPlaceholderText("Regular expression")
        self.regex_input.setClearButtonEnabled(True)
        self.regex_input.textChanged.connect(self._on_filter_changed)
        form.addRow("Path", self.regex_input)
        self.regex_error = QLabel()
        self.regex_error.setWordWrap(True)
        self.regex_error.setVisible(False)
        form.addRow("", self.regex_error)

        self.exclude_input = QLineEdit()
        self.exclude_input.setPlaceholderText("node_modules, *.tmp")
        self.exclude_input.setClearButtonEnabled(True)
        self.exclude_input.textChanged.connect(self._on_filter_changed)
        form.addRow("Exclude", self.exclude_input)

        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if label and field and isinstance(label.widget(), QLabel) and label.widget().text():
                label.widget().setBuddy(field.widget())
                field.widget().setAccessibleName(label.widget().text())

        layout.addLayout(form)
        layout.addStretch()
        self._sync_reset()

    # -- state
    def active_filter_count(self) -> int:
        return sum((
            self.ext_combo.currentIndex() > 0,
            self.size_combo.currentIndex() > 0,
            self.date_combo.currentIndex() > 0,
            bool(self.regex_input.text().strip()),
            bool(self.exclude_input.text().strip()),
        ))

    def _sync_reset(self) -> None:
        self.clear_filters_btn.setEnabled(self.active_filter_count() > 0)

    def update_extensions(self, extensions: List[str]):
        """Fill the type list with the most common extensions, keeping the selection."""
        current = self.ext_combo.currentData()
        self.ext_combo.blockSignals(True)
        self.ext_combo.clear()
        self.ext_combo.addItem("All types", None)
        for ext in extensions:
            if ext:
                self.ext_combo.addItem(ext, ext)
        if current:
            idx = self.ext_combo.findData(current)
            if idx < 0:
                self.ext_combo.addItem(current, current)
                idx = self.ext_combo.count() - 1
            self.ext_combo.setCurrentIndex(idx)
        self.ext_combo.blockSignals(False)

    def _on_filter_changed(self, *_args):
        self._sync_reset()
        self._filter_timer.start()

    def _on_date_preset_changed(self, *_args):
        self.custom_date_widget.setVisible(self.date_combo.currentData() == "custom")
        self._on_filter_changed()

    def reset_filters(self):
        # blockSignals() on the panel does not silence its children, so block each
        # control; otherwise every child fires filters_changed and triggers a search.
        self._filter_timer.stop()
        controls = (self.ext_combo, self.size_combo, self.date_combo,
                    self.regex_input, self.exclude_input)
        for c in controls:
            c.blockSignals(True)
        self.ext_combo.setCurrentIndex(0)
        self.size_combo.setCurrentIndex(0)
        self.date_combo.setCurrentIndex(0)
        self.custom_date_widget.setVisible(False)
        self.regex_input.clear()
        self._set_regex_error(None)
        self.exclude_input.clear()
        for c in controls:
            c.blockSignals(False)
        self._sync_reset()
        self.filters_changed.emit()

    def _set_regex_error(self, message: Optional[str]) -> None:
        if message:
            theme.set_error_text(self.regex_error, True)
        self.regex_error.setText(message or "")
        self.regex_error.setVisible(bool(message))
        self.regex_input.setToolTip(message or "")
        self.regex_input.setAccessibleDescription(message or "")

    def get_filter_values(self) -> dict:
        """Active filters for the search engine."""
        regex_pattern = self.regex_input.text().strip()
        if regex_pattern:
            try:
                re.compile(regex_pattern)
                self._set_regex_error(None)
            except re.error as exc:
                # An invalid pattern must not abort the search; say so and ignore it.
                self._set_regex_error(f"Not a valid regular expression ({exc}). Ignored.")
                regex_pattern = ""
        else:
            self._set_regex_error(None)
        excludes = [p.strip() for p in self.exclude_input.text().split(",") if p.strip()]

        date_range: Optional[Tuple[Optional[float], Optional[float]]] = None
        today = QDate.currentDate()
        now = time.time()
        day = 24 * 3600
        midnight = float(today.startOfDay().toSecsSinceEpoch())
        preset = self.date_combo.currentData()
        if preset == "today":
            date_range = (midnight, now)
        elif preset == "yesterday":
            date_range = (float(today.addDays(-1).startOfDay().toSecsSinceEpoch()), midnight - 1)
        elif preset == "week":
            date_range = (now - 7 * day, now)
        elif preset == "month":
            date_range = (now - 30 * day, now)
        elif preset == "year":
            date_range = (now - 365 * day, now)
        elif preset == "custom":
            d1, d2 = self.start_date_edit.date(), self.end_date_edit.date()
            if d1 > d2:
                d1, d2 = d2, d1
            date_range = (float(d1.startOfDay().toSecsSinceEpoch()),
                          float(d2.endOfDay().toSecsSinceEpoch()))

        return {
            "extension": self.ext_combo.currentData() or None,
            "size_range": self.size_combo.currentData(),
            "date_range": date_range,
            "regex": regex_pattern or None,
            "exclude_patterns": excludes,
        }


# --------------------------------------------------------------------------- preview
class MatchHighlighter(QSyntaxHighlighter):
    """Marks the search terms in the text preview, like find-in-page."""

    def __init__(self, parent: QTextDocument, terms: List[str]):
        super().__init__(parent)
        self.rules: List[Tuple[re.Pattern, QTextCharFormat]] = []
        self._fmt = QTextCharFormat()
        self._fmt.setBackground(QColor("#ffe082"))
        self._fmt.setForeground(QColor("#000000"))
        self._build_rules(terms)

    def _build_rules(self, terms: List[str]) -> None:
        self.rules = []
        for term in terms:
            if len(term) >= 2:
                self.rules.append((re.compile(re.escape(term), re.IGNORECASE), self._fmt))

    def set_terms(self, terms: List[str]) -> None:
        self._build_rules(terms)
        self.rehighlight()

    def highlightBlock(self, text: Optional[str]) -> None:  # noqa: N802
        if not text:
            return
        for pat, fmt in self.rules:
            for match in pat.finditer(text):
                start, end = match.span()
                self.setFormat(start, end - start, fmt)


class PreviewLoaderThread(QThread):
    """Reads a file's start and metadata off the GUI thread, then hashes it.

    Cancelled cooperatively (never ``terminate()``d): killing a thread that holds the
    GIL or an open file handle can deadlock or crash the whole process.
    """
    loaded = Signal(str, str, object)  # path, content_preview, meta
    hashes_ready = Signal(str, str, str)  # path, md5, sha256
    error = Signal(str)

    MAX_HASH_SIZE = 512 * 1024 * 1024

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        self.requestInterruption()

    def _is_cancelled(self) -> bool:
        return self._cancelled or self.isInterruptionRequested()

    @staticmethod
    def _decode_preview(raw: bytes) -> Optional[str]:
        """Decode the start of a file as text, or None when it looks binary."""
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16", errors="replace")
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        if b"\x00" in raw:
            return None
        return raw.decode("utf-8", errors="replace")

    def run(self):
        try:
            p = Path(self.path)
            if not p.exists():
                self.error.emit("The file no longer exists.")
                return

            meta = dict(get_file_metadata(self.path))
            meta.setdefault("extension", p.suffix.lower())
            pending = "Calculating…" if p.is_file() else "Not available for folders"
            meta["md5"] = meta["sha256"] = pending

            mime, _ = mimetypes.guess_type(self.path)
            if p.is_dir():
                try:
                    n = 0
                    for _ in p.iterdir():
                        n += 1
                        if n >= 100000:
                            break
                    count = f"{n:,}+" if n >= 100000 else f"{n:,}"
                except OSError:
                    count = "an unknown number of"
                content = f"Folder with {count} items."
            elif mime and mime.startswith("image/"):
                content = "IMAGE_FILE"
            elif mime and mime.startswith("video/"):
                content = "No preview for video files."
            elif mime == "application/pdf":
                content = "No preview for PDF files. Open the file to read it."
            else:
                try:
                    with open(self.path, "rb") as f:
                        raw = f.read(8192)
                    text = self._decode_preview(raw)
                    content = text[:4000] if text is not None else "No preview for this type of file."
                except OSError as exc:
                    content = f"The file could not be read: {exc.strerror or exc}"

            if self._is_cancelled():
                return
            self.loaded.emit(self.path, content, meta)

            # Hashing can take a while on big files, so it is reported separately and
            # never delays the preview itself.
            if not p.is_file():
                return
            try:
                size = p.stat().st_size
            except OSError:
                return
            if size > self.MAX_HASH_SIZE:
                note = "Not calculated for files over 512 MB"
                self.hashes_ready.emit(self.path, note, note)
                return
            try:
                import hashlib
                h_md5 = hashlib.md5(usedforsecurity=False)
                h_sha256 = hashlib.sha256()
                with open(self.path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        if self._is_cancelled():
                            return
                        h_md5.update(chunk)
                        h_sha256.update(chunk)
                self.hashes_ready.emit(self.path, h_md5.hexdigest(), h_sha256.hexdigest())
            except OSError as e:
                msg = f"Could not read the file: {e.strerror or e}"
                self.hashes_ready.emit(self.path, msg, msg)
        except Exception as e:
            if not self._is_cancelled():
                self.error.emit(str(e))


class PreviewPanel(QFrame):
    """Name, location, a text/image preview and file details for the selection."""

    DETAIL_FIELDS = ("Location", "Size", "Created", "Modified", "MD5", "SHA-256")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PreviewPanel")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self.current_path = ""
        self.loader_thread: Optional[PreviewLoaderThread] = None
        self._retired_threads: List[PreviewLoaderThread] = []
        self.highlighter: Optional[MatchHighlighter] = None
        self.search_terms: List[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)

        self.file_title = ElidedLabel()
        self.file_title.setFont(theme.ui_font(1.1, QFont.Weight.DemiBold))
        self.file_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.file_title)
        self.file_location = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        theme.make_secondary(self.file_location)
        layout.addWidget(self.file_location)
        layout.addSpacing(SPACE_S)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.preview_tab = QWidget()
        preview_layout = QVBoxLayout(self.preview_tab)
        preview_layout.setContentsMargins(0, SPACE_S, 0, 0)
        self.text_preview = QPlainTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setFont(theme.fixed_font())
        self.text_preview.setAccessibleName("File preview")
        self.image_preview = QLabel()
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setVisible(False)
        self.image_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image_preview.setAccessibleName("Image preview")
        preview_layout.addWidget(self.text_preview)
        preview_layout.addWidget(self.image_preview)
        self.tabs.addTab(self.preview_tab, "Preview")

        self.details_tab = QWidget()
        form = QFormLayout(self.details_tab)
        form.setContentsMargins(0, SPACE_M, 0, 0)
        form.setHorizontalSpacing(SPACE_M)
        form.setVerticalSpacing(SPACE_S)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.meta_labels: Dict[str, QLabel] = {}
        for field in self.DETAIL_FIELDS:
            title = SecondaryLabel(field)
            value = QLabel("")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setAccessibleName(field)
            if field in ("MD5", "SHA-256"):
                value.setFont(theme.fixed_font())
            form.addRow(title, value)
            self.meta_labels[field] = value
        self.tabs.addTab(self.details_tab, "Details")
        layout.addWidget(self.tabs, 1)

        self._show_nothing()

        # Never let Qt destroy a QThread that is still running at shutdown.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def _show_nothing(self) -> None:
        self.file_title.setText("No file selected")
        self.file_location.setText("Select a result to preview it.")
        self.text_preview.clear()
        self.text_preview.setPlaceholderText("")
        for lbl in self.meta_labels.values():
            lbl.setText("")

    def clear(self) -> None:
        self._retire_loader()
        self.current_path = ""
        self.image_preview.clear()
        self.image_preview.setVisible(False)
        self.text_preview.setVisible(True)
        self._show_nothing()

    def set_search_terms(self, terms: List[str]):
        self.search_terms = terms
        if self.highlighter is not None:
            self.highlighter.set_terms(terms)

    def _retire_loader(self) -> None:
        """Cancel the running loader without blocking the GUI or killing the thread."""
        t = self.loader_thread
        self.loader_thread = None
        if t is None:
            return
        for sig in (t.loaded, t.hashes_ready, t.error):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        t.cancel()
        if t.isRunning():
            self._retired_threads.append(t)
            t.finished.connect(lambda t=t: self._drop_retired(t))

    def _drop_retired(self, t: PreviewLoaderThread) -> None:
        if t in self._retired_threads:
            self._retired_threads.remove(t)
        t.deleteLater()

    def shutdown(self) -> None:
        """Stop all background loaders and wait for them (call before the app exits)."""
        self._retire_loader()
        for t in list(self._retired_threads):
            t.wait(3000)

    def load_file(self, path: str):
        if not path or path == self.current_path:
            return
        self.current_path = path
        p = Path(path)
        self.file_title.setText(theme.display_text(p.name or path))
        self.file_location.setText(theme.display_text(str(p.parent)))

        self.text_preview.clear()
        self.text_preview.setPlaceholderText("Loading preview…")
        self.image_preview.clear()
        self.image_preview.setVisible(False)
        self.text_preview.setVisible(True)
        for lbl in self.meta_labels.values():
            lbl.setText("")

        self._retire_loader()
        self.loader_thread = PreviewLoaderThread(path)
        self.loader_thread.loaded.connect(self._on_preview_loaded)
        self.loader_thread.hashes_ready.connect(self._on_hashes_ready)
        self.loader_thread.error.connect(self._on_preview_error)
        self.loader_thread.start()

    @staticmethod
    def _format_ts(meta: dict, *keys: str) -> str:
        for key in keys:
            value = meta.get(key)
            if value:
                return theme.format_timestamp_iso(value)
        return "Unknown"

    def _load_image(self, path: str) -> None:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        area = self.preview_tab.size()
        target = QSize(max(area.width() - SPACE_S, 200), max(area.height() - SPACE_S, 200))
        src = reader.size()
        if src.isValid() and (src.width() > target.width() or src.height() > target.height()):
            # Decode straight to the display size: avoids holding a huge bitmap in memory.
            reader.setScaledSize(src.scaled(target, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            self.image_preview.setVisible(False)
            self.text_preview.setVisible(True)
            self.text_preview.setPlainText("This image could not be loaded.")
        else:
            self.image_preview.setPixmap(QPixmap.fromImage(image))

    @Slot(str, str, object)
    def _on_preview_loaded(self, path, content, meta):
        if path != self.current_path:
            return
        self.text_preview.setPlaceholderText("No preview available.")
        size = int(meta.get("size", 0) or 0)
        self.meta_labels["Location"].setText(theme.display_text(str(Path(str(meta.get("path", path))).parent)))
        self.meta_labels["Size"].setText("" if meta.get("is_dir") else theme.format_size_exact(size))
        self.meta_labels["Created"].setText(self._format_ts(meta, "created", "ctime"))
        self.meta_labels["Modified"].setText(self._format_ts(meta, "modified", "mtime"))
        self.meta_labels["MD5"].setText(str(meta.get("md5", "")))
        self.meta_labels["SHA-256"].setText(str(meta.get("sha256", "")))

        if content == "IMAGE_FILE":
            self.text_preview.setVisible(False)
            self.image_preview.setVisible(True)
            self._load_image(path)
        else:
            # Plain text on purpose: rich text would render .html/.md sources.
            self.text_preview.setPlainText(content)
            if self.search_terms:
                if self.highlighter is None:
                    self.highlighter = MatchHighlighter(self.text_preview.document(), self.search_terms)
                else:
                    self.highlighter.set_terms(self.search_terms)

    @Slot(str, str, str)
    def _on_hashes_ready(self, path, md5_hex, sha256_hex):
        if path != self.current_path:
            return
        self.meta_labels["MD5"].setText(md5_hex)
        self.meta_labels["SHA-256"].setText(sha256_hex)

    @Slot(str)
    def _on_preview_error(self, err_msg):
        self.text_preview.setPlaceholderText("")
        self.text_preview.setPlainText(f"No preview: {err_msg}")
        # Allow re-selecting the same file to retry.
        self.current_path = ""


# --------------------------------------------------------------------------- settings
def tray_wording() -> str:
    if theme.IS_MAC:
        return "Keep running in the menu bar when the window is closed"
    if theme.IS_WINDOWS:
        return "Keep running in the notification area when the window is closed"
    return "Keep running in the system tray when the window is closed"


class SettingsDialog(QDialog):
    """Settings (Preferences on macOS): appearance, results, folders, exclusions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(theme.settings_label())
        self.setModal(True)
        self.resize(560, 460)
        self.indexing_changed = False
        cfg = config_manager.config

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_L, SPACE_L, SPACE_L, SPACE_L)
        layout.setSpacing(SPACE_L)
        self.tabs = QTabWidget()
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

        self.max_results_spin = QSpinBox()
        self.max_results_spin.setRange(10, 100000)
        self.max_results_spin.setSingleStep(100)
        self.max_results_spin.setGroupSeparatorShown(True)
        self.max_results_spin.setValue(cfg.max_results)
        form.addRow("Maximum results", self.max_results_spin)

        self.tray_check = QCheckBox(tray_wording())
        self.tray_check.setChecked(theme.setting_bool("close_to_tray", False))
        if QSystemTrayIcon.isSystemTrayAvailable():
            form.addRow("", self.tray_check)
        else:
            self.tray_check.setVisible(False)
        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if label and field and isinstance(label.widget(), QLabel):
                label.widget().setBuddy(field.widget())
                field.widget().setAccessibleName(label.widget().text())
        self.tabs.addTab(general, "General")

        # -- Indexing
        indexing = QWidget()
        col = QVBoxLayout(indexing)
        col.setContentsMargins(SPACE_L, SPACE_L, SPACE_L, SPACE_L)
        col.setSpacing(SPACE_S)
        folders_lbl = QLabel("Folders to index")
        folders_lbl.setFont(theme.ui_font(1.0, QFont.Weight.DemiBold))
        col.addWidget(folders_lbl)
        self.paths_list = QListWidget()
        self.paths_list.setAccessibleName("Folders to index")
        self.paths_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.paths_list.addItems(cfg.index_paths)
        folders_lbl.setBuddy(self.paths_list)
        col.addWidget(self.paths_list, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE_S)
        self.add_path_btn = QPushButton("Add folder…")
        self.add_path_btn.clicked.connect(self.add_indexed_path)
        self.remove_path_btn = QPushButton("Remove")
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
        self.exclude_edit.setPlainText("\n".join(cfg.exclude_patterns))
        self.exclude_edit.setTabChangesFocus(True)
        exclude_lbl.setBuddy(self.exclude_edit)
        col.addWidget(self.exclude_edit, 1)
        col.addWidget(SecondaryLabel("One folder name or pattern per line, for example node_modules or *.tmp."))

        col.addSpacing(SPACE_M)
        self.content_check = QCheckBox("Index text inside files")
        self.content_check.setChecked(bool(getattr(cfg, "index_content", False)))
        col.addWidget(self.content_check)
        col.addWidget(SecondaryLabel("Lets you search file contents. Indexing is slower and the index gets larger."))
        self.tabs.addTab(indexing, "Indexing")

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.save_settings)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    @property
    def appearance(self) -> str:
        return self.appearance_combo.currentData() or "system"

    def add_indexed_path(self):
        folder = QFileDialog.getExistingDirectory(self, "Add folder to index")
        if folder:
            # Qt returns forward slashes on Windows; store native paths and compare
            # case/separator-insensitively so the same folder is never listed twice.
            folder = os.path.normpath(folder)
            for i in range(self.paths_list.count()):
                if paths_equal(self.paths_list.item(i).text(), folder):
                    return
            self.paths_list.addItem(folder)

    def remove_indexed_path(self):
        for s in self.paths_list.selectedItems():
            self.paths_list.takeItem(self.paths_list.row(s))

    def save_settings(self):
        cfg = config_manager.config
        paths = [self.paths_list.item(i).text() for i in range(self.paths_list.count())]
        excludes = [pat.strip() for pat in self.exclude_edit.toPlainText().split("\n") if pat.strip()]
        index_content = self.content_check.isChecked()
        self.indexing_changed = (paths != list(cfg.index_paths)
                                 or excludes != list(cfg.exclude_patterns)
                                 or index_content != bool(getattr(cfg, "index_content", False)))
        old = (list(cfg.index_paths), list(cfg.exclude_patterns), cfg.max_results,
               getattr(cfg, "index_content", False))
        cfg.index_paths = paths
        cfg.exclude_patterns = excludes
        cfg.max_results = self.max_results_spin.value()
        cfg.index_content = index_content
        try:
            config_manager.save()
        except Exception as e:
            cfg.index_paths, cfg.exclude_patterns, cfg.max_results, cfg.index_content = old
            QMessageBox.warning(self, "Settings not saved",
                                f"The settings could not be saved.\n\n{e}")
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
def export_results(parent: QWidget, results: List[dict]) -> Optional[str]:
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
                writer.writerow(["Name", "Extension", "Size", "Modified", "Path", "Score"])
                for item in results:
                    writer.writerow([item.get("name", ""), item.get("extension", ""),
                                     item.get("size", 0), item.get("modified", 0),
                                     item.get("path", ""), item.get("score", 0.0)])
    except OSError as e:
        QMessageBox.warning(parent, "Export failed",
                            f'Could not write "{Path(path).name}": {e.strerror or e}')
        return None
    n = len(results)
    return f'Exported {n:,} {"result" if n == 1 else "results"} to "{Path(path).name}".'
