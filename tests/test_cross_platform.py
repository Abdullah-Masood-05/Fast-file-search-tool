"""Per-platform behaviour of the app layer: per-user dirs, path rules, config root
de-duplication, desktop helpers (reveal / open / trash / clipboard / theme) and the UI's
file actions. Platform branches are exercised by monkeypatching ``sys.platform`` / env
vars and mocking subprocess calls.

The v1 walker, index, history and watcher tests that used to live here are re-covered
against the v2 engine in ``tests/engine`` (test_walker, test_store, test_textfold,
test_exclusions, test_watcher, test_userdata, test_memindex).
"""
import os
import subprocess
import sys
import unicodedata
from pathlib import Path, PurePosixPath

import pytest

from fast_file_search.config import AppConfig
from fast_file_search.utils import platform_utils as pu

IS_WINDOWS = sys.platform == "win32"

NFC_NAME = unicodedata.normalize("NFC", "Café Résumé.txt")
NFD_NAME = unicodedata.normalize("NFD", "Café Résumé.txt")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for var in ("FAST_FILE_SEARCH_CACHE_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    return home


# --------------------------------------------------------------------------- per-user dirs

def test_linux_dirs_follow_xdg(fake_home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert pu.get_app_data_dir("App") == fake_home / ".config" / "App"
    assert pu.get_cache_dir("App") == fake_home / ".cache" / "App"
    assert pu.get_data_dir("App") == fake_home / ".local" / "share" / "App"

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg / "data"))
    assert pu.get_app_data_dir("App") == xdg / "cfg" / "App"
    assert pu.get_cache_dir("App") == xdg / "cache" / "App"
    assert pu.get_data_dir("App") == xdg / "data" / "App"

    # Empty or relative values must be ignored (XDG spec)
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/dir")
    assert pu.get_app_data_dir("App") == fake_home / ".config" / "App"
    assert pu.get_cache_dir("App") == fake_home / ".cache" / "App"


def test_macos_dirs(fake_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    support = fake_home / "Library" / "Application Support"
    assert pu.get_app_data_dir("App") == support / "App"
    assert pu.get_cache_dir("App") == fake_home / "Library" / "Caches" / "App"
    assert pu.get_data_dir("App") == support / "App"


def test_windows_dirs_unchanged(fake_home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert pu.get_app_data_dir("App") == tmp_path / "Roaming" / "App"
    assert pu.get_cache_dir("App") == tmp_path / "Local" / "App"
    # existing Windows history/favorites stay where they were
    assert pu.get_data_dir("App") == pu.get_cache_dir("App")


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_cache_dir_override_wins_everywhere(fake_home, tmp_path, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("FAST_FILE_SEARCH_CACHE_DIR", str(tmp_path / "portable"))
    assert pu.get_cache_dir("App") == tmp_path / "portable" / "App"
    assert pu.get_data_dir("App") == tmp_path / "portable" / "App"


def test_default_index_root_is_home_not_cwd(fake_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pu.get_default_index_root() == str(fake_home)



# --------------------------------------------------------------------------- path rules

def test_windows_paths_case_insensitive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert pu.paths_equal("C:\\Users\\Me\\Docs", "c:/users/me/docs/")
    assert pu.is_path_under("c:\\USERS\\me\\docs\\a.txt", "C:\\Users\\Me")
    assert not pu.is_path_under("C:\\Users\\Mex", "C:\\Users\\Me")
    assert pu.is_path_under("D:\\x", "D:\\")


def test_macos_paths_case_and_normalization_insensitive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pu.paths_equal("/Users/me/" + NFD_NAME, "/users/ME/" + NFC_NAME.upper())
    assert pu.is_path_under("/Users/Me/Docs/x", "/users/me/docs")


def test_linux_paths_case_sensitive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert not pu.paths_equal("/home/A", "/home/a")
    assert not pu.paths_equal("/home/" + NFC_NAME, "/home/" + NFD_NAME)
    assert pu.is_path_under("/a/b/c", "/a/b")
    assert not pu.is_path_under("/a/b\\c", "/a/b")  # backslash is a name character
    assert pu.is_path_under("/anything", "/")


def test_dedupe_roots(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert pu.dedupe_roots(["C:\\Data\\sub", "c:\\data", "C:\\DATA\\", "D:\\x", ""]) == ["c:\\data", "D:\\x"]
    monkeypatch.setattr(sys, "platform", "linux")
    assert pu.dedupe_roots(["/home/A", "/home/a", "/home/a/b"]) == ["/home/A", "/home/a"]


def test_config_dedupes_case_variant_roots(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = AppConfig(index_paths=["C:\\Data", "c:/data/", "", "D:\\x"])
    assert cfg.index_paths == ["C:\\Data", "D:\\x"]
    monkeypatch.setattr(sys, "platform", "linux")
    assert AppConfig(index_paths=["/data", "/Data"]).index_paths == ["/data", "/Data"]


# --------------------------------------------------------------------------- desktop helpers

class _Recorder:
    def __init__(self, returncode=0, stdout=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout

    def run(self, cmd, *args, **kwargs):
        self.calls.append(cmd if isinstance(cmd, str) else list(cmd))
        rc = self.returncode(cmd) if callable(self.returncode) else self.returncode
        out = self.stdout(cmd) if callable(self.stdout) else self.stdout
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    def popen(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))

        class _P:
            returncode = 0

            def communicate(self, *a, **k):
                return (b"", b"")
        return _P()


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(pu.subprocess, "run", rec.run)
    monkeypatch.setattr(pu.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(pu, "_qt_app", lambda: None)
    return rec


def test_reveal_windows(tmp_path, recorder, monkeypatch):
    f = tmp_path / "a,b c.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "win32")
    pu.open_file_manager(f)
    # one quoted token, so commas/spaces in the path cannot split it
    assert recorder.calls == [f'explorer /select,"{f.absolute()}"']


def test_reveal_macos(tmp_path, recorder, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pu.open_file_manager(f) is True
    assert recorder.calls == [["open", "-R", str(f.absolute())]]


def test_reveal_linux_uses_dbus_show_items(tmp_path, recorder, monkeypatch):
    f = tmp_path / "it's, odd.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu.shutil, "which", lambda name: "/usr/bin/" + name)
    assert pu.open_file_manager(f) is True
    cmd = recorder.calls[0]
    assert cmd[0] == "gdbus" and "org.freedesktop.FileManager1.ShowItems" in cmd
    uri_arg = cmd[-2]
    assert cmd[-1] == "''"  # empty startup id in GVariant text syntax
    assert uri_arg.startswith("['file://") and uri_arg.endswith("']")
    assert "'" not in uri_arg[2:-2] and "," not in uri_arg  # quote/comma are escaped


def test_reveal_linux_falls_back_to_xdg_open(tmp_path, recorder, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu.shutil, "which", lambda name: "/usr/bin/" + name)
    recorder.returncode = 1  # no FileManager1 service on the bus
    assert pu.open_file_manager(f) is True
    assert [c[0] for c in recorder.calls] == ["gdbus", "dbus-send", "xdg-open"]
    assert recorder.calls[-1] == ["xdg-open", str(f.absolute().parent)]


def test_reveal_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        pu.open_file_manager(tmp_path / "missing")


def test_file_uri_quoting():
    assert pu._file_uri(PurePosixPath("/tmp/it's a,b.txt")) == "file:///tmp/it%27s%20a%2Cb.txt"
    assert pu._file_uri(PurePosixPath("/tmp/caf\u00e9")) == "file:///tmp/caf%C3%A9"


@pytest.mark.parametrize("platform,expected", [("darwin", ["open"]), ("linux", ["xdg-open"])])
def test_open_path_posix(tmp_path, recorder, monkeypatch, platform, expected):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", platform)
    assert pu.open_path(f) is True
    assert recorder.calls == [expected + [str(f)]]


def test_open_path_windows(tmp_path, recorder, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("x")
    opened = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(pu.os, "startfile", opened.append, raising=False)
    assert pu.open_path(f) is True and opened == [str(f)]


def test_move_to_trash_linux_fallback(tmp_path, recorder, monkeypatch):
    from PySide6.QtCore import QFile

    f = tmp_path / "doomed.txt"
    f.write_text("x")
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda *a: (False, "")))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu.shutil, "which", lambda name: "/usr/bin/" + name if name == "gio" else None)

    def fake_run(cmd, *a, **k):
        recorder.calls.append(list(cmd))
        os.remove(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(pu.subprocess, "run", fake_run)
    assert pu.move_to_trash(f) is True
    assert recorder.calls == [["gio", "trash", "--", str(f)]]
    assert pu.move_to_trash(tmp_path / "missing") is False


def test_move_to_trash_reports_failure(tmp_path, recorder, monkeypatch):
    from PySide6.QtCore import QFile

    f = tmp_path / "kept.txt"
    f.write_text("x")
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda *a: (False, "")))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu.shutil, "which", lambda name: None)
    assert pu.move_to_trash(f) is False and f.exists()  # never deletes permanently


def test_theme_macos(recorder, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    recorder.stdout = "Dark\n"
    assert pu.get_system_theme() == "dark"
    assert recorder.calls[-1] == ["defaults", "read", "-g", "AppleInterfaceStyle"]
    recorder.returncode, recorder.stdout = 1, ""  # key absent in light mode
    assert pu.get_system_theme() == "light"


@pytest.mark.parametrize("scheme,gtk,expected", [
    ("'prefer-dark'", "'Adwaita'", "dark"),
    ("'prefer-light'", "'Adwaita-dark'", "light"),
    ("'default'", "'Adwaita-dark'", "dark"),
    ("'default'", "'Yaru'", "light"),
])
def test_theme_linux_gsettings(recorder, monkeypatch, scheme, gtk, expected):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("GTK_THEME", raising=False)
    recorder.stdout = lambda cmd: scheme if cmd[-1] == "color-scheme" else gtk
    assert pu.get_system_theme() == expected


def test_theme_linux_without_gsettings(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pu, "_qt_app", lambda: None)
    monkeypatch.delenv("GTK_THEME", raising=False)

    def missing(*a, **k):
        raise FileNotFoundError("gsettings")
    monkeypatch.setattr(pu.subprocess, "run", missing)
    assert pu.get_system_theme() == "light"
    monkeypatch.setenv("GTK_THEME", "Adwaita:dark")
    assert pu.get_system_theme() == "dark"


def test_clipboard_prefers_qt(monkeypatch):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def no_subprocess(*a, **k):
        raise AssertionError("subprocess used although Qt is available")
    monkeypatch.setattr(pu.subprocess, "run", no_subprocess)
    monkeypatch.setattr(pu.subprocess, "Popen", no_subprocess)
    assert pu.copy_to_clipboard("hello path") is True
    assert app.clipboard().text() == "hello path"

# --------------------------------------------------------------------------- UI file actions

def test_file_actions_report_missing_paths(tmp_path):
    from fast_file_search.ui import file_actions as fa

    gone = str(tmp_path / "gone.txt")
    for fn in (fa.open_path, fa.reveal, fa.open_with):
        msg = fn(gone)
        assert msg and "gone.txt" in msg and "no longer exists" in msg
    assert fa.move_to_trash(gone)[0] is False


def test_file_actions_go_through_platform_utils(tmp_path, monkeypatch):
    from fast_file_search.ui import file_actions as fa

    f = tmp_path / "a.txt"
    f.write_text("x")
    calls = []
    monkeypatch.setattr(pu, "open_path", lambda p: calls.append(("open", p)) or True)
    monkeypatch.setattr(pu, "open_file_manager", lambda p: calls.append(("reveal", p)) or True)
    monkeypatch.setattr(pu, "move_to_trash", lambda p: calls.append(("trash", p)) or True)
    assert fa.open_path(str(f)) is None and fa.reveal(str(f)) is None
    assert fa.move_to_trash(str(f)) == (True, None)
    assert calls == [("open", str(f)), ("reveal", str(f)), ("trash", str(f))]
    monkeypatch.setattr(pu, "open_path", lambda p: False)
    assert "Could not open" in fa.open_path(str(f))
    monkeypatch.setattr(pu, "move_to_trash", lambda p: False)
    assert fa.move_to_trash(str(f)) == (False, None)   # caller may offer a permanent delete


@pytest.mark.parametrize("platform,gio,mode", [("win32", None, "system"), ("darwin", None, "choose-app"),
                                               ("linux", "/usr/bin/gio", "list"), ("linux", None, "")])
def test_open_with_mode_per_os(monkeypatch, platform, gio, mode):
    from fast_file_search.ui import file_actions as fa

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(fa.shutil, "which", lambda name: gio)
    assert fa.open_with_mode() == mode


def test_open_with_choices_linux_reads_desktop_files(tmp_path, monkeypatch):
    from fast_file_search.ui import file_actions as fa

    apps = tmp_path / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / "org.gnome.TextEditor.desktop").write_text(
        "[Desktop Entry]\nName=Text Editor\nExec=x %U\n", encoding="utf-8")
    (apps / "hidden.desktop").write_text("[Desktop Entry]\nName=Hidden\nNoDisplay=true\n",
                                         encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(fa.shutil, "which", lambda name: "/usr/bin/gio")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_DATA_DIRS", "")
    out = ("Default application for 'text/plain': org.gnome.TextEditor.desktop\n"
           "Registered applications:\n\torg.gnome.TextEditor.desktop\n\thidden.desktop\n"
           "\tmissing.desktop\n")
    monkeypatch.setattr(fa.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=out, stderr=""))
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert fa.open_with_choices(str(f)) == [("Text Editor", str(apps / "org.gnome.TextEditor.desktop"))]


def test_delete_permanently_files_and_folders(tmp_path):
    from fast_file_search.ui import file_actions as fa

    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "f.txt").write_text("x")
    (tmp_path / "g.txt").write_text("x")
    assert fa.delete_permanently(str(tmp_path / "d")) is None
    assert fa.delete_permanently(str(tmp_path / "g.txt")) is None
    assert not (tmp_path / "d").exists() and not (tmp_path / "g.txt").exists()
    assert "Could not delete" in fa.delete_permanently(str(tmp_path / "nope"))


# --------------------------------------------------------------------------- config

def test_config_first_run_only_when_file_missing(tmp_path, monkeypatch):
    from fast_file_search import config as config_mod

    monkeypatch.setattr(config_mod, "get_app_data_dir", lambda *_a, **_k: tmp_path)
    first = config_mod.ConfigManager()
    assert first.first_run is True
    first.config.index_paths = []
    first.save()

    again = config_mod.ConfigManager()
    assert again.first_run is False
    assert again.config.index_paths == []


def test_package_version_matches_pyproject():
    import re
    from pathlib import Path

    import fast_file_search

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == fast_file_search.__version__
