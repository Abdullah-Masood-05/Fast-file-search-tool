from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .config import config_manager
from .ui.main_window import MainWindow
from .ui.widgets import app_icon
from .utils.platform_utils import get_default_index_root


def main():
    # Enable High DPI scaling for crisp visuals
    # In PySide6, High-DPI scaling is enabled by default, but we set style hint for scaling
    app = QApplication(sys.argv)
    app.setApplicationName("Fast File Search Pro")
    # Wayland/X11 match the window to its .desktop entry (icon, taskbar grouping)
    app.setDesktopFileName("fast-file-search-pro")
    
    # Keep the platform's native style (macOS, Windows 11, Breeze/Adwaita...).
    # Only the legacy Windows 10 "windowsvista" style can't draw a dark palette,
    # so fall back to Fusion there.
    style = app.style()
    if style is not None and style.name().lower() == "windowsvista":
        app.setStyle("Fusion")

    # Load configuration settings
    config = config_manager.config
    if config_manager.first_run and not config.index_paths:
        # First run: index the home folder, not the CWD (which is "/" or the
        # .app bundle when launched from a desktop/Finder). Later launches keep
        # an empty folder list empty, so the "no folders" state stays reachable.
        config.index_paths.append(get_default_index_root())
        config_manager.save()
    
    # Initialize main UI window
    window = MainWindow()
    
    # Set main window icon if available
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

        
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
