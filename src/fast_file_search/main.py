"""Application entry point.

Light on purpose (design R1): only ``sys``, ``os`` and ``multiprocessing`` are imported at
module level. A spawned engine process re-runs the entry script's top level, so Qt, the
config and everything else is imported inside ``main()``.
"""
from __future__ import annotations

import multiprocessing
import os
import sys


def parse_args(argv):
    import argparse

    p = argparse.ArgumentParser(prog="fast-file-search-pro", add_help=True,
                                description="Fast File Search Pro")
    p.add_argument("query", nargs="*", help="optional text to search for")
    p.add_argument("--selftest", nargs=2, metavar=("FIXTURE_DIR", "OUT_JSON"),
                   help="index a folder through the engine, check the results, write a JSON report")
    p.add_argument("--selftest-gui", action="store_true",
                   help="create the window offscreen, run one search and quit")
    p.add_argument("--engine-inprocess", action="store_true",
                   help="run the engine as threads in this process (degraded fallback)")
    return p.parse_args(argv)


def _install_crash_dialog(log_dir):
    """Show a non-modal "Something went wrong" box for the first unhandled exception."""
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtWidgets import QMessageBox

    from . import applog

    class Reporter(QObject):
        raised = Signal(str)

        def __init__(self):
            super().__init__()
            self._shown = False
            self._box = None
            self.raised.connect(self._show, Qt.ConnectionType.QueuedConnection)

        def _show(self, details: str) -> None:
            if self._shown:
                return
            self._shown = True
            box = QMessageBox(QMessageBox.Icon.Warning, "Something went wrong",
                              "An unexpected error occurred. The app keeps running.")
            box.setDetailedText(details)
            copy_btn = box.addButton("Copy details", QMessageBox.ButtonRole.ActionRole)
            logs_btn = box.addButton("Show log folder", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Close)

            def clicked(button):
                from .utils.platform_utils import copy_to_clipboard, open_path
                if button is copy_btn:
                    copy_to_clipboard(details)
                elif button is logs_btn:
                    open_path(log_dir)

            box.buttonClicked.connect(clicked)
            box.setModal(False)
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._box = box
            box.show()

    reporter = Reporter()

    def listener(exc_type, exc, tb, thread_name):
        reporter.raised.emit(applog.format_exception(exc_type, exc, tb))

    applog.add_exception_listener(listener)
    return reporter


def _run_gui(args) -> int:
    import logging

    from . import applog
    from .config import build_engine_bootstrap, get_config_manager
    from .utils.platform_utils import get_log_dir, get_state_dir, redirect_qt_settings

    log_dir = get_log_dir()
    redirect_qt_settings()
    applog.setup_logging("gui", log_dir)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    applog.install_qt_message_handler()
    app = QApplication(sys.argv)
    app.setApplicationName("Fast File Search Pro")
    # Wayland/X11 match the window to its .desktop entry (icon, taskbar grouping)
    app.setDesktopFileName("fast-file-search-pro")
    # Keep the platform's native style; only the legacy Windows 10 "windowsvista" style
    # can't draw a dark palette, so fall back to Fusion there.
    style = app.style()
    if style is not None and style.name().lower() == "windowsvista":
        app.setStyle("Fusion")

    from .single_instance import SingleInstance

    query = " ".join(args.query).strip()
    instance = SingleInstance(query, get_state_dir())
    if not instance.acquire():
        return 0  # the running instance was asked to show itself

    cm = get_config_manager()
    first_run = cm.first_run
    notices = cm.take_notices()
    _install_crash_dialog(log_dir)

    from .engine_client import EngineClient
    from .ui.main_window import MainWindow
    from .ui.widgets import app_icon

    boot = build_engine_bootstrap(cm.config)
    client = EngineClient(boot, in_process=args.engine_inprocess)
    window = MainWindow(client, cm, first_run=first_run)
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    def activate(text: str) -> None:
        window.show_window()
        window.focus_search(text or None)

    instance.activated.connect(activate)
    window.show()
    if query:
        window.focus_search(query)

    def after_first_paint() -> None:
        # The window is already visible; the engine starts now (design B.2).
        client.start()
        for notice in notices:
            try:
                client.notice.emit(notice)
            except Exception:
                logging.getLogger(__name__).exception("could not forward notice")

    QTimer.singleShot(0, after_first_paint)

    def cleanup() -> None:
        try:
            client.shutdown()
        finally:
            instance.release()

    app.aboutToQuit.connect(cleanup)
    return app.exec()


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        from .selftest import run_selftest
        return run_selftest(args.selftest[0], args.selftest[1], in_process=args.engine_inprocess)
    if args.selftest_gui:
        from .selftest import run_selftest_gui
        return run_selftest_gui(in_process=args.engine_inprocess)
    return _run_gui(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
