from __future__ import annotations

import os
import re
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QSlider, QDateEdit, QLineEdit, QFileDialog, QDialog, QTabWidget,
    QListWidget, QListWidgetItem, QGroupBox, QFormLayout, QSpinBox,
    QCheckBox, QTextEdit, QProgressBar, QMessageBox, QDoubleSpinBox,
    QDialogButtonBox, QFrame, QGridLayout, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QDate, QPoint, QSize, QThread, pyqtSlot
from PyQt6.QtGui import QIcon, QColor, QFont, QTextCharFormat, QTextDocument, QSyntaxHighlighter, QPixmap

from ..config import AppConfig, config_manager
from ..utils.file_utils import format_size, get_file_metadata


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
        # We will set a generic or actual icon later if available

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
        self.ext_combo.blockSignals(True)
        self.ext_combo.clear()
        self.ext_combo.addItem("All Types")
        for ext in extensions:
            if ext:
                self.ext_combo.addItem(ext)
        self.ext_combo.blockSignals(False)

    def _on_filter_changed(self):
        self.filters_changed.emit()

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
        self.blockSignals(True)
        self.ext_combo.setCurrentIndex(0)
        self.size_slider.setValue(0)
        self.size_lbl.setText("Any Size")
        self.date_combo.setCurrentIndex(0)
        self.custom_date_widget.setVisible(False)
        self.regex_input.clear()
        self.exclude_input.clear()
        self.blockSignals(False)
        self.filters_changed.emit()

    def get_filter_values(self) -> dict:
        """Returns active filters for search engine queries."""
        ext = self.ext_combo.currentText()
        size_idx = self.size_slider.value()
        date_preset = self.date_combo.currentText()
        regex_pattern = self.regex_input.text().strip()
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
        now = QDate.currentDate()
        import time
        t_now = time.time()
        one_day = 24 * 3600

        if date_preset == "Today":
            date_range = (t_now - one_day, t_now)
        elif date_preset == "Yesterday":
            date_range = (t_now - 2 * one_day, t_now - one_day)
        elif date_preset == "Past Week":
            date_range = (t_now - 7 * one_day, t_now)
        elif date_preset == "Past Month":
            date_range = (t_now - 30 * one_day, t_now)
        elif date_preset == "Past Year":
            date_range = (t_now - 365 * one_day, t_now)
        elif date_preset == "Custom Range":
            start_dt = self.start_date_edit.date().startOfDay().toSecsSinceEpoch()
            end_dt = self.end_date_edit.date().endOfDay().toSecsSinceEpoch()
            date_range = (start_dt, end_dt)

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
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#FFE082"))  # Soft amber background
        fmt.setForeground(QColor("#000000"))
        fmt.setFontWeight(QFont.Weight.Bold)

        for term in terms:
            if len(term) >= 2:
                # Compile regex for safety, ignore case
                pat = re.compile(re.escape(term), re.IGNORECASE)
                self.rules.append((pat, fmt))

    def highlightBlock(self, text: str):
        for pat, fmt in self.rules:
            for match in pat.finditer(text):
                start, end = match.span()
                self.setFormat(start, end - start, fmt)


class PreviewLoaderThread(QThread):
    """Background loader thread to read and preview files safely."""
    loaded = pyqtSignal(str, str, dict)  # path, content_preview, meta
    error = pyqtSignal(str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            p = Path(self.path)
            if not p.exists():
                self.error.emit("File does not exist.")
                return

            meta = get_file_metadata(self.path)

            # Compute MD5 + SHA256 in this background thread (never blocks GUI)
            md5_hex = "N/A (directory)"
            sha256_hex = "N/A (directory)"
            if p.is_file() and p.stat().st_size <= 512 * 1024 * 1024:  # skip files > 512 MB
                try:
                    import hashlib
                    h_md5 = hashlib.md5()
                    h_sha256 = hashlib.sha256()
                    with open(self.path, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h_md5.update(chunk)
                            h_sha256.update(chunk)
                    md5_hex = h_md5.hexdigest()
                    sha256_hex = h_sha256.hexdigest()
                except Exception as e:
                    md5_hex = f"Error: {e}"
                    sha256_hex = f"Error: {e}"
            elif p.is_file():
                md5_hex = sha256_hex = "File too large (>512 MB)"

            meta["md5"] = md5_hex
            meta["sha256"] = sha256_hex

            # Simple content preview loading
            mime, _ = mimetypes.guess_type(self.path)
            if p.is_dir():
                content = f"Directory: {p.name}\nContains {len(list(p.glob('*')))} items."
            elif mime and mime.startswith("image/"):
                content = "IMAGE_FILE"
            elif mime and mime.startswith("video/"):
                content = "VIDEO_FILE"
            elif mime == "application/pdf":
                content = "PDF_FILE"
            else:
                try:
                    with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read(4000)
                except Exception:
                    content = "Binary or unreadable file."

            self.loaded.emit(self.path, content, meta)
        except Exception as e:
            self.error.emit(str(e))


class PreviewPanel(QFrame):
    """Tabbed preview pane displaying file summaries, image thumbs, metadata, tags."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("PreviewPanel")

        self.current_path = ""
        self.loader_thread: Optional[PreviewLoaderThread] = None
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

        if self.loader_thread and self.loader_thread.isRunning():
            self.loader_thread.terminate()

        self.progress_bar.setVisible(True)
        
        self.loader_thread = PreviewLoaderThread(path)
        self.loader_thread.loaded.connect(self._on_preview_loaded)
        self.loader_thread.error.connect(self._on_preview_error)
        self.loader_thread.start()

    @pyqtSlot(str, str, dict)
    def _on_preview_loaded(self, path, content, meta):
        if path != self.current_path:
            return
        self.progress_bar.setVisible(False)

        # Populate Metadata fields
        self.meta_labels["Path"].setText(meta.get("path", ""))
        self.meta_labels["Size"].setText(format_size(meta.get("size", 0)))
        
        import datetime
        try:
            m_time = datetime.datetime.fromtimestamp(meta.get("modified", 0)).strftime('%Y-%m-%d %H:%M:%S')
            c_time = datetime.datetime.fromtimestamp(meta.get("created", 0)).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            m_time = c_time = "Unknown"

        self.meta_labels["Created"].setText(c_time)
        self.meta_labels["Modified"].setText(m_time)
        self.meta_labels["MD5 Hash"].setText(meta.get("md5", "—"))
        self.meta_labels["SHA256 Hash"].setText(meta.get("sha256", "—"))

        # Render Content Preview
        if content == "IMAGE_FILE":
            self.text_preview.setVisible(False)
            self.image_preview.setVisible(True)
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(self.image_preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.image_preview.setPixmap(scaled)
            else:
                self.image_preview.setText("Failed to load image.")
        elif content == "VIDEO_FILE":
            self.text_preview.setText(f"Video File: {Path(path).name}\nType: {meta.get('extension', '')}\nDimensions & duration playback require media extensions.")
        elif content == "PDF_FILE":
            self.text_preview.setText(f"PDF Document: {Path(path).name}\nOpen the file with custom tools to read document content.")
        else:
            self.text_preview.setText(content)
            # Apply highlighting rules
            if self.search_terms:
                self.highlighter = MatchHighlighter(self.text_preview.document(), self.search_terms)

    @pyqtSlot(str)
    def _on_preview_error(self, err_msg):
        self.progress_bar.setVisible(False)
        self.text_preview.setText(f"Error loading preview: {err_msg}")


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
            # Check for duplicate
            for i in range(self.paths_list.count()):
                if self.paths_list.item(i).text() == folder:
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
                with open(path, "w", newline="", encoding="utf-8") as f:
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
