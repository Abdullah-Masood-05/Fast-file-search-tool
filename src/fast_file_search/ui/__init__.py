"""PySide6 user interface (GUI process only; never imports sqlite3)."""
from .main_window import MainWindow
from .widgets import AboutDialog, SettingsDialog

__all__ = ["MainWindow", "SettingsDialog", "AboutDialog"]
