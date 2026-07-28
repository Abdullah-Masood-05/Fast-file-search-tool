import sys
import pytest
from PyQt6.QtWidgets import QApplication
from fast_file_search.ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    # Setup standard QApplication instance
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def test_ui_components_instantiation(qapp):
    window = MainWindow()
    assert window is not None
    assert window.title_bar is not None
    assert window.browser_view is not None
    assert window.search_view is not None
    assert window.filter_panel is not None
    assert window.preview_panel is not None
    
    # Test title label on custom title bar
    assert window.title_bar.title_label.text() == "Fast File Search Pro"
    
    # Verify default state
    assert window.status_lbl.text() == "Ready" or "Indexing" in window.status_lbl.text()
    
    # Clean up window
    window.close()
