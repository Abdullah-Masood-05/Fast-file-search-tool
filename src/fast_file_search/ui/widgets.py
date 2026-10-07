from __future__ import annotations

import os
import re
import sys
import mimetypes
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QSlider, QDateEdit, QLineEdit, QFileDialog, QDialog, QTabWidget,
    QListWidget, QListWidgetItem, QGroupBox, QFormLayout, QSpinBox,
    QCheckBox, QTextEdit, QProgressBar, QMessageBox, QDoubleSpinBox,
    QDialogButtonBox, QFrame, QGridLayout, QSizePolicy, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal, QDate, QPoint, QSize, QThread, QTimer, pyqtSlot
from PyQt6.QtGui import (
    QIcon, QColor, QFont, QTextCharFormat, QTextDocument, QSyntaxHighlighter, QPixmap,
    QImageReader,
)

from ..config import AppConfig, config_manager
from ..utils.file_utils import format_size, get_file_metadata


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


class CustomTitleBar(QWidget):
    """Custom title bar for a frameless/modern look."""
    theme_toggled = pyqtSignal(str)  # Emits new theme name

    def __init__(self, parent=None, title="Fast File Search Pro"):
        super().__init__(parent)
        self.parent_window = parent
        self.drag_position = QPoint()
        self.is_dark = config_manager.config.theme == "dark"

        self.setFixedHeight(42)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(10, 0, 10, 0)
        self.layout.setSpacing(5)

        # App Icon & Title
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(20, 20)
        self.icon_label.setScaledContents(True)
        icon = app_icon()
        if not icon.isNull():
            self.icon_label.setPixmap(icon.pixmap(20, 20))

        self.title_label = QLabel(title)
        self.title_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.title_label.setStyleSheet("color: white; border: none; background: transparent;")

        self.layout.addWidget(self.icon_label)
        self.layout.addWidget(self.title_label)
        self.layout.addStretch()

        # Theme Toggle Button
        self.theme_btn = QPushButton()
        self.theme_btn.setFixedSize(30, 30)
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.clicked.connect(self.toggle_theme)
        self.update_theme_btn_icon()
        self.layout.addWidget(self.theme_btn)

        # Window Controls
        self.min_btn = QPushButton("—")
        self.min_btn.setFixedSize(35, 30)
        self.min_btn.clicked.connect(self.minimize_parent)

        self.max_btn = QPushButton("⬜")
        self.max_btn.setFixedSize(35, 30)
        self.max_btn.clicked.connect(self.maximize_parent)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(35, 30)
        self.close_btn.setObjectName("close_btn")
        self.close_btn.clicked.connect(self.close_parent)

        for btn in (self.min_btn, self.max_btn, self.close_btn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.layout.addWidget(btn)

        self.setStyleSheet("""
            CustomTitleBar {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1E1E24, stop:1 #2D2D35);
                border-bottom: 1px solid #15151A;
            }
            QPushButton {
                background: transparent;
                border: none;
                color: #DCDCDC;
                font-family: 'Segoe UI';
                font-size: 11px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background: #3E3E45;
                color: white;
            }
            QPushButton#close_btn:hover {
                background: #E81123;
                color: white;
            }
        """)

    def update_theme_btn_icon(self):
        # Using unicode symbols for light/dark theme toggle to avoid missing image file issues
        self.theme_btn.setText("☀️" if self.is_dark else "🌙")
        self.theme_btn.setStyleSheet("""
            QPushButton {
                font-size: 14px;
                color: white;
                background: transparent;
                border: none;
            }
            QPushButton:hover {
                background: #3E3E45;
                border-radius: 15px;
            }
        """)

    def toggle_theme(self):
        self.is_dark = not self.is_dark
        theme_str = "dark" if self.is_dark else "light"
        config_manager.config.theme = theme_str
        config_manager.save()
        self.update_theme_btn_icon()
        self.theme_toggled.emit(theme_str)

    def minimize_parent(self):
        if self.parent_window:
            self.parent_window.showMinimized()

    def maximize_parent(self):
        if self.parent_window:
            if self.parent_window.isMaximized():
                self.parent_window.showNormal()
                self.max_btn.setText("⬜")
            else:
                self.parent_window.showMaximized()
                self.max_btn.setText("❐")

    def close_parent(self):
        if self.parent_window:
            self.parent_window.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.parent_window:
            self.drag_position = event.globalPosition().toPoint() - self.parent_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.parent_window:
            self.parent_window.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_parent()


class FilterPanel(QFrame):
    """Collapsible panel containing advanced filters."""
    filters_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("FilterPanel")
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(12)

        # Typing in the regex/exclude boxes must not fire a full search per keystroke.
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(300)
        self._filter_timer.timeout.connect(self.filters_changed.emit)

        # Title / Collapse Header
        self.header_layout = QHBoxLayout()
        self.title_lbl = QLabel("Advanced Filters")
        self.title_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.header_layout.addWidget(self.title_lbl)
        self.header_layout.addStretch()
        
        self.clear_filters_btn = QPushButton("Reset")
        self.clear_filters_btn.setFixedSize(50, 22)
        self.clear_filters_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_filters_btn.clicked.connect(self.reset_filters)
        self.header_layout.addWidget(self.clear_filters_btn)
        self.main_layout.addLayout(self.header_layout)

        # Line Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("background-color: #DDDDDD;")
        self.main_layout.addWidget(line)

        # Filters Layout Form
        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setSpacing(10)
        self.form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 1. File Type/Extension Selector
        self.ext_combo = QComboBox()
        self.ext_combo.addItem("All Types")
        self.ext_combo.currentTextChanged.connect(self._on_filter_changed)
        self.form_layout.addRow(QLabel("File Type:"), self.ext_combo)

        # 2. File Size Selector
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(0, 6)
        self.size_slider.setValue(0)
        self.size_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.size_slider.setTickInterval(1)
        self.size_slider.valueChanged.connect(self._on_size_slider_changed)

        self.size_lbl = QLabel("Any Size")
        self.size_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.Medium))
        self.size_lbl.setStyleSheet("color: #666;")

        size_container = QVBoxLayout()
        size_container.addWidget(self.size_slider)
        size_container.addWidget(self.size_lbl)
        self.form_layout.addRow(QLabel("Max Size:"), size_container)

        # 3. Date Modified Range Selector
        self.date_combo = QComboBox()
        self.date_combo.addItems(["Any Time", "Today", "Yesterday", "Past Week", "Past Month", "Past Year", "Custom Range"])
        self.date_combo.currentTextChanged.connect(self._on_date_preset_changed)
        self.form_layout.addRow(QLabel("Date Modified:"), self.date_combo)

        # Custom date widget container (hidden by default)
        self.custom_date_widget = QWidget()
        date_range_layout = QHBoxLayout(self.custom_date_widget)
        date_range_layout.setContentsMargins(0, 0, 0, 0)
        self.start_date_edit = QDateEdit(QDate.currentDate().addYears(-1))
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.dateChanged.connect(self._on_filter_changed)
        self.end_date_edit = QDateEdit(QDate.currentDate())
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.dateChanged.connect(self._on_filter_changed)

        date_range_layout.addWidget(self.start_date_edit)
        date_range_layout.addWidget(QLabel("to"))
        date_range_layout.addWidget(self.end_date_edit)
        self.custom_date_widget.setVisible(False)
        self.form_layout.addRow("", self.custom_date_widget)

        # 4. Path Regex Filter
        self.regex_input = QLineEdit()
        self.regex_input.setPlaceholderText("^.*\\\\src\\\\.*$")
        self.regex_input.textChanged.connect(self._on_filter_changed)
        self.form_layout.addRow(QLabel("Path Regex:"), self.regex_input)

        # 5. Exclude Patterns
        self.exclude_input = QLineEdit()
        self.exclude_input.setPlaceholderText(".git, __pycache__, node_modules")
        self.exclude_input.textChanged.connect(self._on_filter_changed)
        self.form_layout.addRow(QLabel("Exclude:"), self.exclude_input)

        self.main_layout.addWidget(self.form_widget)
        self.main_layout.addStretch()

        # Apply initial (light) theme
        self.apply_theme(False)

    def apply_theme(self, is_dark: bool):
        """Apply dark or light stylesheet to the entire filter panel."""
        if is_dark:
            self.setStyleSheet("""
                QFrame#FilterPanel {
                    background-color: #1C1C1E;
                    border-right: 1px solid #2C2C2E;
                }
                QLabel {
                    font-family: 'Segoe UI';
                    font-size: 11px;
                    color: #EBEBF5;
                }
                QComboBox, QLineEdit, QDateEdit {
                    font-family: 'Segoe UI';
                    font-size: 11px;
                    padding: 4px 8px;
                    border: 1px solid #3A3A3C;
                    border-radius: 4px;
                    background-color: #2C2C2E;
                    color: #FFFFFF;
                }
                QComboBox:hover, QLineEdit:focus, QDateEdit:hover {
                    border-color: #0A84FF;
                }
                QComboBox QAbstractItemView {
                    background-color: #2C2C2E;
                    color: #FFFFFF;
                    selection-background-color: #0A84FF;
                }
                QSlider::groove:horizontal {
                    height: 4px;
                    background: #3A3A3C;
                    border-radius: 2px;
                }
                QSlider::handle:horizontal {
                    background: #0A84FF;
                    width: 14px;
                    height: 14px;
                    margin: -5px 0;
                    border-radius: 7px;
                }
                QSlider::sub-page:horizontal {
                    background: #0A84FF;
                    border-radius: 2px;
                }
                QPushButton {
                    font-family: 'Segoe UI';
                    font-size: 10px;
                    background-color: #2C2C2E;
                    color: #FFFFFF;
                    border: 1px solid #3A3A3C;
                    border-radius: 3px;
                }
                QPushButton:hover {
                    background-color: #3A3A3C;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame#FilterPanel {
                    background-color: #F8F9FA;
                    border-right: 1px solid #E0E0E0;
                }
                QLabel {
                    font-family: 'Segoe UI';
                    font-size: 11px;
                    color: #333333;
                }
                QComboBox, QLineEdit, QDateEdit {
                    font-family: 'Segoe UI';
                    font-size: 11px;
                    padding: 4px 8px;
                    border: 1px solid #CCCCCC;
                    border-radius: 4px;
                    background-color: #FFFFFF;
                    color: #333333;
                }
                QComboBox:hover, QLineEdit:focus, QDateEdit:hover {
                    border-color: #007AFF;
                }
                QSlider::groove:horizontal {
                    height: 4px;
                    background: #CCCCCC;
                    border-radius: 2px;
                }
                QSlider::handle:horizontal {
                    background: #007AFF;
                    width: 14px;
                    height: 14px;
                    margin: -5px 0;
                    border-radius: 7px;
                }
                QSlider::sub-page:horizontal {
                    background: #007AFF;
                    border-radius: 2px;
                }
                QPushButton {
                    font-family: 'Segoe UI';
                    font-size: 10px;
                    background-color: #EFEFEF;
                    color: #333333;
                    border: 1px solid #CCCCCC;
                    border-radius: 3px;
                }
                QPushButton:hover {
                    background-color: #DDDDDD;
                }
            """)

    def update_extensions(self, extensions: List[str]):
        """Populate the file type dropdown dynamically."""
        current = self.ext_combo.currentText()
        self.ext_combo.blockSignals(True)
        self.ext_combo.clear()
        self.ext_combo.addItem("All Types")
        for ext in extensions:
            if ext:
                self.ext_combo.addItem(ext)
        # Keep the user's selection across index refreshes.
        if current and current != "All Types":
            idx = self.ext_combo.findText(current)
            if idx < 0:
                self.ext_combo.addItem(current)
                idx = self.ext_combo.count() - 1
            self.ext_combo.setCurrentIndex(idx)
        self.ext_combo.blockSignals(False)

    def _on_filter_changed(self, *_args):
        self._filter_timer.start()

    def _on_size_slider_changed(self, value):
        labels = {
            0: "Any Size",
            1: "Tiny (< 10 KB)",
            2: "Small (10 KB - 1 MB)",
            3: "Medium (1 MB - 10 MB)",
            4: "Large (10 MB - 100 MB)",
            5: "Huge (100 MB - 1 GB)",
            6: "Gigantic (> 1 GB)"
        }
        self.size_lbl.setText(labels.get(value, "Any Size"))
        self._on_filter_changed()

    def _on_date_preset_changed(self, text):
        self.custom_date_widget.setVisible(text == "Custom Range")
        self._on_filter_changed()

    def reset_filters(self):
        # QObject.blockSignals() on the panel does not silence its children, so block each
        # control; otherwise every child fires filters_changed and triggers its own search.
        self._filter_timer.stop()
        controls = (
            self.ext_combo, self.size_slider, self.date_combo,
            self.regex_input, self.exclude_input,
        )
        for c in controls:
            c.blockSignals(True)
        self.ext_combo.setCurrentIndex(0)
        self.size_slider.setValue(0)
        self.size_lbl.setText("Any Size")
        self.date_combo.setCurrentIndex(0)
        self.custom_date_widget.setVisible(False)
        self.regex_input.clear()
        self.regex_input.setStyleSheet("")
        self.exclude_input.clear()
        for c in controls:
            c.blockSignals(False)
        self.filters_changed.emit()

    def get_filter_values(self) -> dict:
        """Returns active filters for search engine queries."""
        ext = self.ext_combo.currentText()
        size_idx = self.size_slider.value()
        date_preset = self.date_combo.currentText()
        regex_pattern = self.regex_input.text().strip()
        if regex_pattern:
            try:
                re.compile(regex_pattern)
                self.regex_input.setStyleSheet("")
                self.regex_input.setToolTip("")
            except re.error as exc:
                # An invalid pattern must not abort the whole search; flag it and ignore it.
                self.regex_input.setStyleSheet("QLineEdit { border: 1px solid #D32F2F; }")
                self.regex_input.setToolTip(f"Invalid regular expression: {exc}")
                regex_pattern = ""
        else:
            self.regex_input.setStyleSheet("")
        excludes = [p.strip() for p in self.exclude_input.text().split(",") if p.strip()]

        size_range: Optional[Tuple[Optional[int], Optional[int]]] = None
        # Values mapping in Bytes
        if size_idx == 1:
            size_range = (0, 10 * 1024)
        elif size_idx == 2:
            size_range = (10 * 1024, 1024 * 1024)
        elif size_idx == 3:
            size_range = (1024 * 1024, 10 * 1024 * 1024)
        elif size_idx == 4:
            size_range = (10 * 1024 * 1024, 100 * 1024 * 1024)
        elif size_idx == 5:
            size_range = (100 * 1024 * 1024, 1024 * 1024 * 1024)
        elif size_idx == 6:
            size_range = (1024 * 1024 * 1024, None)

        date_range: Optional[Tuple[Optional[float], Optional[float]]] = None
        today = QDate.currentDate()
        t_now = time.time()
        one_day = 24 * 3600
        midnight = float(today.startOfDay().toSecsSinceEpoch())

        if date_preset == "Today":
            date_range = (midnight, t_now)
        elif date_preset == "Yesterday":
            date_range = (float(today.addDays(-1).startOfDay().toSecsSinceEpoch()), midnight - 1)
        elif date_preset == "Past Week":
            date_range = (t_now - 7 * one_day, t_now)
        elif date_preset == "Past Month":
            date_range = (t_now - 30 * one_day, t_now)
        elif date_preset == "Past Year":
            date_range = (t_now - 365 * one_day, t_now)
        elif date_preset == "Custom Range":
            d1, d2 = self.start_date_edit.date(), self.end_date_edit.date()
            if d1 > d2:
                d1, d2 = d2, d1
            date_range = (
                float(d1.startOfDay().toSecsSinceEpoch()),
                float(d2.endOfDay().toSecsSinceEpoch()),
            )

        return {
            "extension": None if ext == "All Types" else ext,
            "size_range": size_range,
            "date_range": date_range,
            "regex": regex_pattern if regex_pattern else None,
            "exclude_patterns": excludes
        }


class MatchHighlighter(QSyntaxHighlighter):
    """QSyntaxHighlighter to highlight search terms matching current queries."""
    def __init__(self, parent: QTextDocument, terms: List[str]):
        super().__init__(parent)
        self.rules: List[Tuple[re.Pattern, QTextCharFormat]] = []
        self._fmt = QTextCharFormat()
        self._fmt.setBackground(QColor("#FFE082"))  # Soft amber background
        self._fmt.setForeground(QColor("#000000"))
        self._fmt.setFontWeight(QFont.Weight.Bold)
        self._build_rules(terms)

    def _build_rules(self, terms: List[str]) -> None:
        self.rules = []
        for term in terms:
            if len(term) >= 2:
                # Escape wildcards/regex metacharacters; ignore case
                self.rules.append((re.compile(re.escape(term), re.IGNORECASE), self._fmt))

    def set_terms(self, terms: List[str]) -> None:
        self._build_rules(terms)
        self.rehighlight()

    def highlightBlock(self, text: Optional[str]) -> None:
        if not text:
            return
        for pat, fmt in self.rules:
            for match in pat.finditer(text):
                start, end = match.span()
                self.setFormat(start, end - start, fmt)


class PreviewLoaderThread(QThread):
    """Background loader thread to read and preview files safely.

    The thread is cancelled cooperatively (never ``terminate()``d): killing a thread that
    holds the GIL or an open file handle can deadlock or crash the whole process.
    """
    loaded = pyqtSignal(str, str, dict)  # path, content_preview, meta
    hashes_ready = pyqtSignal(str, str, str)  # path, md5, sha256
    error = pyqtSignal(str)

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
                self.error.emit("File does not exist.")
                return

            meta = dict(get_file_metadata(self.path))
            meta.setdefault("extension", p.suffix.lower())
            meta["md5"] = "Calculating..." if p.is_file() else "N/A (directory)"
            meta["sha256"] = meta["md5"]

            # Simple content preview loading
            mime, _ = mimetypes.guess_type(self.path)
            if p.is_dir():
                try:
                    n = 0
                    for _ in p.iterdir():
                        n += 1
                        if n >= 100000:
                            break
                    count = f"{n}+" if n >= 100000 else str(n)
                except OSError:
                    count = "?"
                content = f"Directory: {p.name}\nContains {count} items."
            elif mime and mime.startswith("image/"):
                content = "IMAGE_FILE"
            elif mime and mime.startswith("video/"):
                content = "VIDEO_FILE"
            elif mime == "application/pdf":
                content = "PDF_FILE"
            else:
                try:
                    with open(self.path, "rb") as f:
                        raw = f.read(8192)
                    text = self._decode_preview(raw)
                    content = text[:4000] if text is not None else "Binary file - no text preview."
                except Exception:
                    content = "Binary or unreadable file."

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
                note = "File too large (>512 MB)"
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
            except Exception as e:
                msg = f"Error: {e}"
                self.hashes_ready.emit(self.path, msg, msg)
        except Exception as e:
            if not self._is_cancelled():
                self.error.emit(str(e))


class PreviewPanel(QFrame):
    """Tabbed preview pane displaying file summaries, image thumbs, metadata, tags."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("PreviewPanel")

        self.current_path = ""
        self.loader_thread: Optional[PreviewLoaderThread] = None
        self._retired_threads: List[PreviewLoaderThread] = []
        self.highlighter: Optional[MatchHighlighter] = None
        self.search_terms: List[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Header Title
        self.file_title = QLabel("Select a file to preview")
        self.file_title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.file_title.setWordWrap(True)
        layout.addWidget(self.file_title)

        # Tabs
        self.tabs = QTabWidget()
        
        # Preview Tab
        self.preview_tab = QWidget()
        preview_layout = QVBoxLayout(self.preview_tab)
        preview_layout.setContentsMargins(0, 5, 0, 0)
        
        self.text_preview = QTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setFont(QFont("Consolas", 9))
        self.text_preview.setPlaceholderText("No preview available.")
        
        self.image_preview = QLabel()
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setVisible(False)
        self.image_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

        preview_layout.addWidget(self.text_preview)
        preview_layout.addWidget(self.image_preview)
        self.tabs.addTab(self.preview_tab, "Preview")

        # Details Tab
        self.details_tab = QWidget()
        self.details_layout = QGridLayout(self.details_tab)
        self.details_layout.setContentsMargins(10, 10, 10, 10)
        self.details_layout.setSpacing(10)
        
        self.meta_labels: Dict[str, QLabel] = {}
        fields = [("Path", 0), ("Size", 1), ("Created", 2), ("Modified", 3), ("MD5 Hash", 4), ("SHA256 Hash", 5)]
        for label, row in fields:
            lbl_title = QLabel(f"{label}:")
            lbl_title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            lbl_val = QLabel("")
            lbl_val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl_val.setWordWrap(True)
            self.details_layout.addWidget(lbl_title, row, 0, Qt.AlignmentFlag.AlignTop)
            self.details_layout.addWidget(lbl_val, row, 1)
            self.meta_labels[label] = lbl_val

        self.details_layout.setColumnStretch(1, 1)
        # SHA256 is long — let it wrap naturally
        if "SHA256 Hash" in self.meta_labels:
            self.meta_labels["SHA256 Hash"].setWordWrap(True)
        self.details_layout.setRowStretch(len(fields), 1)
        self.tabs.addTab(self.details_tab, "Details")

        layout.addWidget(self.tabs)

        # Loading / Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFixedHeight(3)
        layout.addWidget(self.progress_bar)

        self.apply_theme(False)

        # Never let Qt destroy a QThread that is still running at shutdown.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def apply_theme(self, is_dark: bool):
        """Apply dark or light stylesheet to the preview panel."""
        if is_dark:
            self.setStyleSheet("""
                QFrame#PreviewPanel {
                    background-color: #1C1C1E;
                    border-left: 1px solid #2C2C2E;
                }
                QTabWidget::pane {
                    border: 1px solid #2C2C2E;
                    background-color: #18181A;
                }
                QTabBar::tab {
                    font-family: 'Segoe UI';
                    font-size: 10px;
                    padding: 6px 12px;
                    background: #2C2C2E;
                    color: #EBEBF5;
                    border: 1px solid #3A3A3C;
                    border-bottom: none;
                    border-top-left-radius: 4px;
                    border-top-right-radius: 4px;
                }
                QTabBar::tab:selected {
                    background: #18181A;
                    color: #FFFFFF;
                    border-color: #3A3A3C;
                }
                QTextEdit {
                    border: none;
                    background: #18181A;
                    color: #EBEBF5;
                    font-family: 'Consolas';
                    font-size: 9px;
                }
                QLabel {
                    font-family: 'Segoe UI';
                    font-size: 10px;
                    color: #EBEBF5;
                }
                QProgressBar {
                    background: #2C2C2E;
                    border: none;
                }
                QProgressBar::chunk {
                    background: #0A84FF;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame#PreviewPanel {
                    background-color: #F8F9FA;
                    border-left: 1px solid #E0E0E0;
                }
                QTabWidget::pane {
                    border: 1px solid #DDDDDD;
                    background-color: #FFFFFF;
                }
                QTabBar::tab {
                    font-family: 'Segoe UI';
                    font-size: 10px;
                    padding: 6px 12px;
                    background: #EFEFEF;
                    border: 1px solid #CCCCCC;
                    border-bottom: none;
                    border-top-left-radius: 4px;
                    border-top-right-radius: 4px;
                }
                QTabBar::tab:selected {
                    background: #FFFFFF;
                    border-color: #DDDDDD;
                }
                QTextEdit {
                    border: none;
                    background: #FFFFFF;
                    color: #222222;
                }
                QLabel {
                    font-family: 'Segoe UI';
                    font-size: 10px;
                    color: #333333;
                }
                QProgressBar {
                    background: #E0E0E0;
                    border: none;
                }
                QProgressBar::chunk {
                    background: #007AFF;
                }
            """)

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
        self.file_title.setText(Path(path).name)

        # Clean preview states
        self.text_preview.clear()
        self.image_preview.clear()
        self.image_preview.setVisible(False)
        self.text_preview.setVisible(True)

        self._retire_loader()

        self.progress_bar.setVisible(True)

        self.loader_thread = PreviewLoaderThread(path)
        self.loader_thread.loaded.connect(self._on_preview_loaded)
        self.loader_thread.hashes_ready.connect(self._on_hashes_ready)
        self.loader_thread.error.connect(self._on_preview_error)
        self.loader_thread.start()

    @staticmethod
    def _format_ts(meta: dict, *keys: str) -> str:
        import datetime
        for key in keys:
            value = meta.get(key)
            if value:
                try:
                    return datetime.datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M:%S')
                except (OverflowError, OSError, ValueError, TypeError):
                    break
        return "Unknown"

    def _load_image(self, path: str) -> None:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        area = self.preview_tab.size()
        target = QSize(max(area.width() - 10, 200), max(area.height() - 10, 200))
        src = reader.size()
        if src.isValid() and (src.width() > target.width() or src.height() > target.height()):
            # Decode straight to the display size: avoids holding a huge bitmap in memory.
            reader.setScaledSize(src.scaled(target, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            self.image_preview.setText("Failed to load image.")
        else:
            self.image_preview.setPixmap(QPixmap.fromImage(image))

    @pyqtSlot(str, str, dict)
    def _on_preview_loaded(self, path, content, meta):
        if path != self.current_path:
            return
        self.progress_bar.setVisible(False)

        # Populate Metadata fields
        self.meta_labels["Path"].setText(str(meta.get("path", path)))
        self.meta_labels["Size"].setText(format_size(int(meta.get("size", 0) or 0)))
        self.meta_labels["Created"].setText(self._format_ts(meta, "created", "ctime"))
        self.meta_labels["Modified"].setText(self._format_ts(meta, "modified", "mtime"))
        self.meta_labels["MD5 Hash"].setText(str(meta.get("md5", "—")))
        self.meta_labels["SHA256 Hash"].setText(str(meta.get("sha256", "—")))

        # Render Content Preview
        if content == "IMAGE_FILE":
            self.text_preview.setVisible(False)
            self.image_preview.setVisible(True)
            self._load_image(path)
        elif content == "VIDEO_FILE":
            self.text_preview.setPlainText(f"Video File: {Path(path).name}\nType: {meta.get('extension', '')}\nDimensions & duration playback require media extensions.")
        elif content == "PDF_FILE":
            self.text_preview.setPlainText(f"PDF Document: {Path(path).name}\nOpen the file with custom tools to read document content.")
        else:
            # Plain text on purpose: setText() would render .html/.md sources as rich text.
            self.text_preview.setPlainText(content)
            if self.search_terms:
                if self.highlighter is None:
                    self.highlighter = MatchHighlighter(self.text_preview.document(), self.search_terms)
                else:
                    self.highlighter.set_terms(self.search_terms)

    @pyqtSlot(str, str, str)
    def _on_hashes_ready(self, path, md5_hex, sha256_hex):
        if path != self.current_path:
            return
        self.meta_labels["MD5 Hash"].setText(md5_hex)
        self.meta_labels["SHA256 Hash"].setText(sha256_hex)

    @pyqtSlot(str)
    def _on_preview_error(self, err_msg):
        self.progress_bar.setVisible(False)
        self.text_preview.setPlainText(f"Error loading preview: {err_msg}")
        # Allow re-selecting the same file to retry.
        self.current_path = ""


class SettingsDialog(QDialog):
    """Configuration Settings management dialog."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(550, 420)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)

        # Tab view
        self.tabs = QTabWidget()

        # Tab 1: Indexing Paths
        self.paths_tab = QWidget()
        paths_layout = QVBoxLayout(self.paths_tab)
        
        self.paths_list = QListWidget()
        self.paths_list.addItems(config_manager.config.index_paths)
        paths_layout.addWidget(QLabel("Indexed Folders:"))
        paths_layout.addWidget(self.paths_list)

        paths_buttons = QHBoxLayout()
        self.add_path_btn = QPushButton("Add Folder...")
        self.add_path_btn.clicked.connect(self.add_indexed_path)
        self.remove_path_btn = QPushButton("Remove Selected")
        self.remove_path_btn.clicked.connect(self.remove_indexed_path)
        paths_buttons.addWidget(self.add_path_btn)
        paths_buttons.addWidget(self.remove_path_btn)
        paths_buttons.addStretch()
        paths_layout.addLayout(paths_buttons)
        self.tabs.addTab(self.paths_tab, "Indexed Folders")

        # Tab 2: Performance
        self.perf_tab = QWidget()
        perf_form = QFormLayout(self.perf_tab)
        
        self.auto_index_spin = QSpinBox()
        self.auto_index_spin.setRange(10, 3600)
        self.auto_index_spin.setValue(config_manager.config.auto_index_interval)
        perf_form.addRow(QLabel("Auto Indexing Interval (sec):"), self.auto_index_spin)

        self.max_size_spin = QSpinBox()
        self.max_size_spin.setRange(50, 10000)
        self.max_size_spin.setValue(config_manager.config.max_index_size)
        perf_form.addRow(QLabel("Max Index Size (MB):"), self.max_size_spin)

        self.max_results_spin = QSpinBox()
        self.max_results_spin.setRange(10, 5000)
        self.max_results_spin.setValue(config_manager.config.max_results)
        perf_form.addRow(QLabel("Max Search Results:"), self.max_results_spin)
        self.tabs.addTab(self.perf_tab, "Performance")

        # Tab 3: Exclude Patterns
        self.exclude_tab = QWidget()
        exclude_layout = QVBoxLayout(self.exclude_tab)
        exclude_layout.addWidget(QLabel("Global Exclude Patterns (one per line):"))
        self.exclude_edit = QTextEdit()
        self.exclude_edit.setPlainText("\n".join(config_manager.config.exclude_patterns))
        exclude_layout.addWidget(self.exclude_edit)
        self.tabs.addTab(self.exclude_tab, "Exclude Patterns")

        layout.addWidget(self.tabs)

        # Dialog Buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.save_settings)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.setStyleSheet("""
            QDialog {
                background-color: #F8F9FA;
            }
            QTabWidget::pane {
                border: 1px solid #CCCCCC;
                border-radius: 4px;
                padding: 10px;
                background-color: #FFFFFF;
            }
            QLineEdit, QSpinBox, QListWidget, QTextEdit {
                border: 1px solid #CCCCCC;
                border-radius: 4px;
                padding: 5px;
                font-family: 'Segoe UI';
                font-size: 11px;
            }
            QPushButton {
                font-family: 'Segoe UI';
                font-size: 11px;
                padding: 5px 12px;
                background-color: #007AFF;
                color: white;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #0056B3;
            }
        """)

    def add_indexed_path(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Index")
        if folder:
            # Qt returns forward slashes on Windows; store native paths and compare
            # case/separator-insensitively so the same folder is never listed twice.
            folder = os.path.normpath(folder)
            key = os.path.normcase(folder)
            for i in range(self.paths_list.count()):
                if os.path.normcase(os.path.normpath(self.paths_list.item(i).text())) == key:
                    return
            self.paths_list.addItem(folder)

    def remove_indexed_path(self):
        selected = self.paths_list.selectedItems()
        for s in selected:
            self.paths_list.takeItem(self.paths_list.row(s))

    def save_settings(self):
        paths = [self.paths_list.item(i).text() for i in range(self.paths_list.count())]
        excludes = [pat.strip() for pat in self.exclude_edit.toPlainText().split("\n") if pat.strip()]

        config_manager.config.index_paths = paths
        config_manager.config.exclude_patterns = excludes
        config_manager.config.auto_index_interval = self.auto_index_spin.value()
        config_manager.config.max_index_size = self.max_size_spin.value()
        config_manager.config.max_results = self.max_results_spin.value()
        
        try:
            config_manager.save()
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save configuration: {e}")


class AboutDialog(QDialog):
    """About dialog with project metadata."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Fast File Search Pro")
        self.setFixedSize(380, 240)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Fast File Search Pro")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        desc = QLabel("A local file search utility that indexes your folders in the background using SQLite trigrams. Includes real-time directory monitoring and advanced size and extension filters.")
        desc.setWordWrap(True)
        desc.setFont(QFont("Segoe UI", 10))
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)


        details = QLabel("Version: 1.0.0\nFramework: PyQt6 (Qt 6)\nLicense: MIT License")
        details.setFont(QFont("Segoe UI", 9.5))
        details.setStyleSheet("color: #666;")
        details.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(details)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setFixedSize(80, 28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.setStyleSheet("""
            QDialog {
                background-color: #FFFFFF;
            }
            QPushButton {
                font-family: 'Segoe UI';
                background-color: #007AFF;
                color: white;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #0056B3;
            }
        """)


class ExportDialog(QDialog):
    """Search results export to JSON/CSV formatted dialog."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Search Results")
        self.setFixedSize(300, 160)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Export formats:"))
        
        self.csv_radio = QPushButton("Export as CSV...")
        self.csv_radio.clicked.connect(self.export_csv)
        self.json_radio = QPushButton("Export as JSON...")
        self.json_radio.clicked.connect(self.export_json)

        layout.addWidget(self.csv_radio)
        layout.addWidget(self.json_radio)
        layout.addStretch()

        self.results_data: List[dict] = []

    def set_results(self, data: List[dict]):
        self.results_data = data

    def export_csv(self):
        if not self.results_data:
            QMessageBox.warning(self, "Warning", "No search results to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV file", "", "CSV Files (*.csv)")
        if path:
            import csv
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Name", "Extension", "Size", "Modified", "Path", "Score"])
                    for item in self.results_data:
                        writer.writerow([
                            item.get("name", ""),
                            item.get("extension", ""),
                            item.get("size", 0),
                            item.get("modified", 0),
                            item.get("path", ""),
                            item.get("score", 0.0)
                        ])
                QMessageBox.information(self, "Export Complete", "Results exported successfully to CSV.")
                self.accept()
            except Exception as e:
                QMessageBox.critical(self, "Export Failed", f"Failed to save CSV file: {e}")

    def export_json(self):
        if not self.results_data:
            QMessageBox.warning(self, "Warning", "No search results to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save JSON file", "", "JSON Files (*.json)")
        if path:
            import json
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.results_data, f, indent=2, ensure_ascii=False)
                QMessageBox.information(self, "Export Complete", "Results exported successfully to JSON.")
                self.accept()
            except Exception as e:
                QMessageBox.critical(self, "Export Failed", f"Failed to save JSON file: {e}")
