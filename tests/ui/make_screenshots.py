"""Render the v2 main window offscreen with the fake engine (light and dark).

    uv run python tests/ui/make_screenshots.py [out_dir]     (default dist/ui_screens)

All app paths (config, cache, QSettings) are redirected to a temp sandbox first.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="ffs-shots-"))
for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
             "XDG_DATA_HOME", "XDG_STATE_HOME", "FAST_FILE_SEARCH_CACHE_DIR"):
    p = SANDBOX / name.lower()
    p.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(p)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QRectF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def _gift_png(path: Path, size: int = 128) -> None:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 16
    p.setPen(QPen(QColor("#3a3a3a"), s * 0.9))
    p.setBrush(QColor("#f2c14e"))
    p.drawRoundedRect(QRectF(2.5 * s, 6.5 * s, 11 * s, 8 * s), s, s)
    p.setBrush(QColor("#e07a5f"))
    p.drawRoundedRect(QRectF(1.5 * s, 4 * s, 13 * s, 3 * s), s * 0.6, s * 0.6)
    p.drawLine(int(8 * s), int(4 * s), int(8 * s), int(14.5 * s))
    p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path))


def main(out: Path) -> int:
    import fake_engine as fe
    from fast_file_search.ui import theme
    from fast_file_search.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion") if app.style().name().lower() == "windowsvista" else None
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(SANDBOX / "qsettings"))
    out.mkdir(parents=True, exist_ok=True)
    home = SANDBOX / "Users" / "alex"
    ds = fe.sample_dataset(str(home))
    for e in ds.entries:
        path = Path(ds.path_of(e))
        if e.flags & 1:
            path.mkdir(parents=True, exist_ok=True)
        elif e.root_id == 1 and e.name.endswith(".png"):
            _gift_png(path)
        elif e.root_id == 1:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * min(e.size, 2048))
    # Pad the per-root entry counts toward the reference numbers.
    names = {1: "Downloads", 2: "Desktop", 3: "Documents", 4: "Projects"}

    def shoot(mode: str, tag: str, setup) -> None:
        QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, theme.APP_NAME, "ui").setValue(
            "appearance", mode)
        client = fe.FakeEngineClient(ds, ready=False)
        status = ds.status(names=names)
        roots = tuple(r.__class__(**{**r.__dict__, "entries": n}) for r, n in
                      zip(status.roots, (16_221, 8_742, 12_391, 3_273)))
        status = status.__class__(**{**status.__dict__, "roots": roots, "total_entries": 57_627})
        client.make_ready(status)
        cm = fe.FakeConfigManager(list(ds.roots.values()))
        win = MainWindow(client, cm)
        win.resize(1280, 800)
        win.show()
        QTest.qWaitForWindowExposed(win)
        win.activateWindow()
        QTest.qWait(50)
        setup(win)
        QTest.qWait(250)
        target = out / f"v2_{mode}_{tag}.png"
        ok = win.grab().save(str(target))
        print(target, ok)
        win._shutdown()
        win.close()
        win.deleteLater()
        app.processEvents()

    def results(win):
        win.sidebar.list.setCurrentIndex(win.sidebar.model.index(0))   # Downloads
        win.chips._update(size_key="tiny", excludes=("node_modules", "*.tmp"))
        f = win.bar.field
        f.setFocus()
        QTest.keyClicks(f, "GiftTopIcon")
        QTest.keyClick(f, Qt.Key.Key_Return)
        QTest.qWait(150)
        win.results.select_row(0)
        win.results.view.setFocus()

    def suggestions(win):
        ds.history = ["gift card receipts", "gifttopicon size:<10kb"]
        f = win.bar.field
        f.setFocus()
        QTest.keyClicks(f, "gift")
        QTest.qWait(400)

    def ready(win):
        pass

    def nomatch(win):
        win.chips._update(type_key="image", date_key="week")
        f = win.bar.field
        f.setFocus()
        QTest.keyClicks(f, "quarterly budget")
        QTest.keyClick(f, Qt.Key.Key_Return)

    for mode in ("light", "dark"):
        shoot(mode, "results", results)
        shoot(mode, "suggestions", suggestions)
        shoot(mode, "ready", ready)
        shoot(mode, "nomatch", nomatch)
    theme.apply_appearance("system")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "dist" / "ui_screens"
    try:
        sys.exit(main(target))
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
