"""Log/state dirs and default roots per OS (platform and environment are monkeypatched)."""
import sys
from pathlib import Path

import pytest

from fast_file_search.utils import platform_utils as pu


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.delenv("FAST_FILE_SEARCH_CACHE_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    return home


def test_log_dir_windows(env, tmp_path, monkeypatch):
    monkeypatch.setattr(pu, "_system", lambda: "Windows")
    assert pu.get_log_dir() == tmp_path / "local" / "Fast File Search Pro" / "Logs"
    assert pu.get_log_dir().is_dir()


def test_log_dir_macos(env, monkeypatch):
    monkeypatch.setattr(pu, "_system", lambda: "Darwin")
    assert pu.get_log_dir() == env / "Library" / "Logs" / "Fast File Search Pro"


def test_log_dir_linux(env, tmp_path, monkeypatch):
    monkeypatch.setattr(pu, "_system", lambda: "Linux")
    assert pu.get_log_dir() == tmp_path / "state" / "fast-file-search-pro" / "logs"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/ignored")
    assert pu.get_log_dir() == env / ".local" / "state" / "fast-file-search-pro" / "logs"


def test_override_redirects_logs_and_state(env, tmp_path, monkeypatch):
    monkeypatch.setenv("FAST_FILE_SEARCH_CACHE_DIR", str(tmp_path / "portable"))
    for system in ("Windows", "Darwin", "Linux"):
        monkeypatch.setattr(pu, "_system", lambda s=system: s)
        assert pu.get_log_dir() == tmp_path / "portable" / "Fast File Search Pro" / "logs"
        assert pu.get_state_dir().is_relative_to(tmp_path / "portable")
        assert pu.get_cache_dir("Fast File Search Pro").is_relative_to(tmp_path / "portable")
        assert pu.get_data_dir("Fast File Search Pro").is_relative_to(tmp_path / "portable")


def test_state_dir_per_os(env, tmp_path, monkeypatch):
    monkeypatch.setattr(pu, "_system", lambda: "Linux")
    assert pu.get_state_dir() == tmp_path / "state" / "fast-file-search-pro"
    monkeypatch.setattr(pu, "_system", lambda: "Darwin")
    assert pu.get_state_dir() == env / "Library" / "Application Support" / "Fast File Search Pro"


def test_default_roots_macos(env, monkeypatch):
    monkeypatch.setattr(pu, "_system", lambda: "Darwin")
    for name in ("Desktop", "Documents", "Movies", "Library"):
        (env / name).mkdir()
    assert pu.default_roots() == [str(env / n) for n in ("Desktop", "Documents", "Movies")]


def test_default_roots_linux_user_dirs(env, tmp_path, monkeypatch):
    monkeypatch.setattr(pu, "_system", lambda: "Linux")
    docs = tmp_path / "Dokumente"
    docs.mkdir()
    (env / "Downloads").mkdir()
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    (cfg / "user-dirs.dirs").write_text(
        f'# comment\nXDG_DOCUMENTS_DIR="{docs}"\nXDG_DOWNLOAD_DIR="$HOME/Downloads"\n'
        'XDG_VIDEOS_DIR="$HOME/Gone"\n', encoding="utf-8")
    assert pu.default_roots() == [str(Path(p)) for p in (docs, env / "Downloads")]


def test_default_roots_never_home(env, monkeypatch):
    monkeypatch.setattr(pu, "_system", lambda: "Linux")
    assert pu.default_roots() == []  # nothing exists: not the home folder either


def test_default_roots_windows_known_folders(env, tmp_path, monkeypatch):
    monkeypatch.setattr(pu, "_system", lambda: "Windows")
    onedrive = tmp_path / "OneDrive" / "Documents"
    onedrive.mkdir(parents=True)
    pics = onedrive / "Pictures"  # nested in Documents: dropped
    pics.mkdir()
    mapping = {pu._USER_FOLDERS[1][1]: str(onedrive), pu._USER_FOLDERS[3][1]: str(pics)}
    monkeypatch.setattr(pu, "_known_folder_windows", lambda guid: mapping.get(guid))
    assert pu.default_roots() == [str(onedrive)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows KnownFolders")
def test_known_folder_windows_real():
    path = pu._known_folder_windows(pu._USER_FOLDERS[1][1])
    if path is None:  # the test sandbox replaces USERPROFILE, which the shell API rejects
        pytest.skip("KnownFolders unavailable with a sandboxed profile")
    assert Path(path).is_absolute()


def test_existing_helpers_still_exported():
    for name in ("open_file_manager", "open_path", "move_to_trash", "path_key", "paths_equal",
                 "is_path_under", "dedupe_roots", "get_app_data_dir", "get_cache_dir",
                 "get_data_dir", "normalize_path", "get_system_theme", "copy_to_clipboard",
                 "get_log_dir", "get_state_dir", "default_roots"):
        assert callable(getattr(pu, name))
