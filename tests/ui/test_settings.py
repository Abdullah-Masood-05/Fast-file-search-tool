"""Settings dialog (F.11), About, export, shared widgets and theme helpers."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtGui import QFontInfo, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton

from fast_file_search.ui import theme
from fast_file_search.ui.widgets import (
    AboutDialog, EmptyState, NoticeBar, SettingsDialog, app_version, export_results, find_resource, glyph_icon,
)

from fake_engine import *  # noqa: F401,F403
from fake_engine import FakeConfigManager


@pytest.fixture
def cm(ui_settings):
    return FakeConfigManager(["C:\\Data" if sys.platform == "win32" else "/data"])


def test_settings_dialog_detects_equivalent_paths(qapp, cm, tmp_path, monkeypatch):
    dlg = SettingsDialog(cm)
    dlg.paths_list.clear()
    forward = str(tmp_path).replace("\\", "/")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: forward))
    dlg.add_indexed_path()
    dlg.add_indexed_path()
    assert dlg.paths() == [os.path.normpath(forward)]
    if sys.platform in ("win32", "darwin"):
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: forward.upper()))
        dlg.add_indexed_path()
        assert len(dlg.paths()) == 1


def test_settings_dialog_saves_v2_keys_and_reports_indexing_changes(qapp, cm, tmp_path):
    dlg = SettingsDialog(cm)
    assert dlg.windowTitle() == theme.settings_label()
    dlg.appearance_combo.setCurrentIndex(dlg.appearance_combo.findData("dark"))
    dlg.save_settings()
    assert not dlg.indexing_changed and cm.saves == 1
    assert theme.ui_settings().value("appearance") == "dark"

    dlg = SettingsDialog(cm)
    dlg._add_path_item(str(tmp_path))
    dlg.checks["live_monitoring"].setChecked(False)
    dlg.checks["index_hidden"].setChecked(False)
    dlg.exclude_edit.setPlainText("node_modules\n*.tmp\n\nnode_modules")
    dlg.save_settings()
    assert dlg.indexing_changed
    assert cm.config.index_paths[-1] == str(tmp_path)
    assert cm.config.exclude_patterns == ["node_modules", "*.tmp"]
    assert cm.config.live_monitoring is False and cm.config.index_hidden is False
    assert not hasattr(cm.config, "max_results")
    theme.ui_settings().remove("appearance")


def test_settings_dialog_rejects_relative_paths(qapp, cm):
    dlg = SettingsDialog(cm)
    dlg._add_path_item("relative/folder")
    dlg.save_settings()
    assert dlg.error_lbl.isVisible() or "full folder path" in dlg.error_lbl.text()
    assert cm.saves == 0


def test_settings_dialog_shows_root_states_and_tools(qapp, cm):
    path = cm.config.index_paths[0]
    rebuilt, logs = [], []
    dlg = SettingsDialog(cm, root_states={path: ("denied", 0)}, on_rebuild=lambda: rebuilt.append(1),
                         on_show_logs=lambda: logs.append(1))
    assert "Access denied" in dlg.paths_list.item(0).text()
    dlg.rebuild_btn.click()
    dlg.logs_btn.click()
    assert rebuilt == [1] and logs == [1]
    mac_only = [dlg.checks["include_home_library"], dlg.checks["descend_packages"]]
    assert all(c.isVisibleTo(dlg) == theme.IS_MAC for c in mac_only)


def test_onboarding_dialog_lists_defaults(qapp, tmp_path, monkeypatch):
    from fast_file_search.ui import onboarding

    a = tmp_path / "Downloads"
    a.mkdir()
    monkeypatch.setattr(onboarding, "default_roots", lambda: [str(a)])
    dlg = onboarding.OnboardingDialog()
    assert dlg.folders() == [str(a)] and dlg.start_btn.isEnabled()
    dlg.boxes[0].setChecked(False)
    assert not dlg.start_btn.isEnabled()
    assert dlg.fda_btn.isVisibleTo(dlg) == theme.IS_MAC


def test_about_dialog_shows_version(qapp):
    dlg = AboutDialog()
    assert app_version() in dlg.version_lbl.text()


def test_export_writes_csv_and_json(qapp, tmp_path, monkeypatch):
    rows = [{"name": f"f{i}.txt", "folder": "/p", "size": i, "modified": 1, "path": f"/p/f{i}.txt", "kind": "TXT"}
            for i in range(3)]
    target = tmp_path / "out.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "CSV (*.csv)")))
    assert "Exported 3 results" in export_results(None, rows)
    assert target.read_text(encoding="utf-8-sig").splitlines()[0].startswith("Name,")
    target = tmp_path / "out"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "JSON (*.json)")))
    export_results(None, rows)
    assert len(json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))) == 3
    assert export_results(None, []) == "There are no results to export."


def test_empty_state_replaces_buttons_without_leftovers(qapp):
    es = EmptyState()
    es.resize(500, 400)
    es.show()
    clicked = []
    es.set_content("a", "Title A", "Message", [("One", lambda: clicked.append(1)), ("Two", lambda: None)])
    es.set_content("b", "Title B", "", [("Three", lambda: clicked.append(3))])
    qapp.processEvents()
    visible = [b.text() for b in es.findChildren(QPushButton) if not b.isHidden()]
    assert visible == ["Three"]
    assert es.kind == "b" and es.message.isHidden()
    es.buttons[0].click()
    assert clicked == [3]


def test_notice_bar_keys_dismiss_and_actions(qapp):
    bar = NoticeBar()
    hits = []
    bar.post("a", "First", [("Fix", lambda: hits.append("a"))])
    bar.post("b", "Second")
    assert bar.isVisibleTo(bar.parentWidget() or bar) or not bar.isHidden()
    assert bar.text.text() == "Second" and bar.buttons == []
    bar.dismiss_current()
    assert bar.text.text() == "First"
    bar.buttons[0].click()
    assert hits == ["a"]
    bar.remove("a")
    assert bar.isHidden()
    bar.post("b", "Again")
    assert bar.isHidden()                       # dismissed keys stay quiet
    bar.post("b", "Forced", force=True)
    assert not bar.isHidden()


def test_secondary_text_meets_contrast_in_light_and_dark(qapp):
    label = theme.SecondaryLabel("x")
    try:
        for mode in ("light", "dark"):
            theme.apply_appearance(mode)
            qapp.processEvents()
            pal = QApplication.palette()
            fg = label.palette().color(QPalette.ColorRole.WindowText)
            assert theme.contrast_ratio(fg, pal.color(QPalette.ColorRole.Window)) >= 4.5
            assert theme.is_dark() == (mode == "dark")
            for state in ("active", "error", "paused"):
                assert theme.status_dot_color(state).isValid()
    finally:
        theme.apply_appearance("system")


def test_glyph_icons_render_in_both_schemes(qapp):
    try:
        for mode in ("light", "dark"):
            theme.apply_appearance(mode)
            for name in ("search", "close", "clock", "star", "image", "document", "code", "gear", "warning", "check"):
                pm = glyph_icon(name).pixmap(16, 16)
                img = pm.toImage()
                assert not pm.isNull()
                assert any(img.pixelColor(x, y).alpha() for x in range(16) for y in range(16)), name
    finally:
        theme.apply_appearance("system")


def test_formatting_helpers():
    assert theme.format_size(512) == "512 B"
    assert theme.format_size(0) == "0 B"
    assert theme.format_date_short(0) == ""
    assert theme.display_text("a\tb\nc") == "a?b?c"
    assert theme.reveal_label() in ("Show in Finder", "Show in File Explorer", "Show in File Manager")
    assert theme.chip_text("Type", "All") == "Type: All"
    assert len(theme.chip_text("Exclude", "x" * 60)) == 28


def test_find_resource_locates_icon_and_misses_cleanly():
    icon = find_resource("files.ico")
    assert icon is not None and icon.name == "files.ico"
    assert find_resource("definitely-not-bundled.xyz") is None


def test_fixed_font_is_monospace_offscreen():
    """theme.fixed_font() must be a real monospace face even where the platform's
    "fixed" system font is proportional (offscreen with QT_QPA_FONTDIR set)."""
    code = ("import os, sys\n"
            "from PySide6.QtWidgets import QApplication\n"
            "from PySide6.QtGui import QFontInfo\n"
            "app = QApplication([])\n"
            "from fast_file_search.ui import theme\n"
            "print(QFontInfo(theme.fixed_font()).fixedPitch())\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    if sys.platform == "win32":
        env["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "True", out.stderr
