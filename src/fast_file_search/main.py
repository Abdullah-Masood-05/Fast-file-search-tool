from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from .config import config_manager
from .ui.main_window import MainWindow


def main():
    # Enable High DPI scaling for crisp visuals
    # In PyQt6, High-DPI scaling is enabled by default, but we set style hint for scaling
    app = QApplication(sys.argv)
    
    # Enable style formatting
    app.setStyle("Fusion")
    
    # Load configuration settings
    config = config_manager.config
    
    # Initialize main UI window
    window = MainWindow()
    
    # Set main window icon if available
    icon_path = Path(__file__).parents[2] / "files.ico"
    if icon_path.exists():
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))
        
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
