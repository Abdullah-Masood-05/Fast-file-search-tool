"""First-run folder picker (design G.6). On macOS it is a sheet on the main window
and explains the one-time privacy prompts; elsewhere it is a plain dialog."""
from __future__ import annotations

import os
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..utils.platform_utils import paths_equal
from . import theme
from .theme import SPACE_L, SPACE_M, SPACE_S, SPACE_XL, SecondaryLabel

FDA_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"

MAC_TEXT = ("macOS asks once for each protected folder (Desktop, Documents, Downloads). "
            "If you prefer one permission for everything, grant Full Disk Access in System Settings.")


def default_roots() -> List[str]:
    try:
        from ..utils import platform_utils

        getter = getattr(platform_utils, "default_roots", None)
        if getter is not None:
            return [p for p in getter() if p]
    except Exception:
        pass
    home = os.path.expanduser("~")
    names = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Movies" if theme.IS_MAC else "Videos")
    return [os.path.join(home, n) for n in names if os.path.isdir(os.path.join(home, n))]


def open_privacy_settings(full_disk: bool = False) -> None:
    QDesktopServices.openUrl(QUrl(FDA_URL if full_disk else PRIVACY_URL))


class OnboardingDialog(QDialog):
    """Lists the default folders as checkboxes plus "Add folder…". ``folders()`` is
    the chosen list once accepted ([Start indexing])."""

    def __init__(self, parent: Optional[QWidget] = None, preselected: Sequence[str] = ()):
        super().__init__(parent)
        self.setWindowTitle("Choose folders to index")
        if theme.IS_MAC:
            self.setWindowFlag(Qt.WindowType.Sheet, True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        col = QVBoxLayout(self)
        col.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_L)
        col.setSpacing(SPACE_S)
        title = QLabel("Choose the folders to search")
        title.setFont(theme.ui_font(1.25, QFont.Weight.DemiBold))
        col.addWidget(title)
        intro = SecondaryLabel("File names in these folders are indexed on this computer. "
                               "File contents are not read.")
        intro.setWordWrap(True)
        col.addWidget(intro)
        if theme.IS_MAC:
            mac = SecondaryLabel(MAC_TEXT)
            mac.setWordWrap(True)
            col.addWidget(mac)
        col.addSpacing(SPACE_M)

        self.boxes: List[QCheckBox] = []
        self.box_col = QVBoxLayout()
        self.box_col.setSpacing(SPACE_S)
        col.addLayout(self.box_col)
        pre = list(preselected)
        candidates = default_roots()
        for p in pre:
            if not any(paths_equal(p, c) for c in candidates):
                candidates.append(p)
        for path in candidates:
            self._add_box(path, checked=not pre or any(paths_equal(path, q) for q in pre))
        self.add_btn = QPushButton("Add folder…")
        self.add_btn.setAccessibleName("Add folder")
        self.add_btn.setAutoDefault(False)
        self.add_btn.clicked.connect(self.add_folder)
        col.addWidget(self.add_btn, 0, Qt.AlignmentFlag.AlignLeft)
        col.addSpacing(SPACE_L)

        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE_S)
        self.fda_btn = QPushButton("Open Full Disk Access settings")
        self.fda_btn.setAccessibleName("Open Full Disk Access settings")
        self.fda_btn.setAutoDefault(False)
        self.fda_btn.clicked.connect(lambda: open_privacy_settings(True))
        self.fda_btn.setVisible(theme.IS_MAC)
        buttons.addWidget(self.fda_btn)
        buttons.addStretch(1)
        self.later_btn = QPushButton("Not now")
        self.later_btn.setAccessibleName("Not now")
        self.later_btn.setAutoDefault(False)
        self.later_btn.clicked.connect(self.reject)
        buttons.addWidget(self.later_btn)
        self.start_btn = QPushButton("Start indexing")
        self.start_btn.setAccessibleName("Start indexing")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self.accept)
        buttons.addWidget(self.start_btn)
        col.addLayout(buttons)
        self._sync()
        self.setMinimumWidth(480)

    def _add_box(self, path: str, checked: bool = True) -> None:
        name = os.path.basename(os.path.normpath(path)) or path
        box = QCheckBox(theme.display_text(name))
        box.setToolTip(path)
        box.setAccessibleName(name)
        box.setAccessibleDescription(path)
        box.setProperty("path", path)
        box.setChecked(checked)
        box.toggled.connect(self._sync)
        self.box_col.addWidget(box)
        self.boxes.append(box)

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add folder to index")
        if not folder:
            return
        folder = os.path.normpath(folder)
        for b in self.boxes:
            if paths_equal(b.property("path"), folder):
                b.setChecked(True)
                return
        self._add_box(folder)
        self._sync()

    def _sync(self) -> None:
        self.start_btn.setEnabled(bool(self.folders()))

    def folders(self) -> List[str]:
        return [b.property("path") for b in self.boxes if b.isChecked()]
