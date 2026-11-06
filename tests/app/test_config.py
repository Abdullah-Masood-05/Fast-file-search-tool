"""Config v2: validation, v1 migration fixtures, corrupt-file recovery, lazy singleton."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fast_file_search import config as cfgmod
from fast_file_search.config import (
    DEFAULT_EXCLUDES,
    AppConfig,
    ConfigManager,
    build_engine_bootstrap,
    get_config_manager,
)
from fast_file_search.utils.platform_utils import APP_DIR_NAME, get_app_data_dir

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(h))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return h


def cfg_file() -> Path:
    d = get_app_data_dir(APP_DIR_NAME)
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.json"


V1_BASE = {
    "index_paths": [], "exclude_patterns": [".git", "__pycache__"], "max_index_size": 500,
    "auto_index_interval": 300, "theme": "dark", "max_results": 1000,
    "search_history": ["a", "b"], "index_content": False,
}


def test_import_has_no_side_effects(tmp_path):
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "ad"),
           "LOCALAPPDATA": str(tmp_path / "lad"), "XDG_CONFIG_HOME": str(tmp_path / "xc"),
           "PYTHONPATH": SRC, "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    code = "import fast_file_search.config as c; print(hasattr(c, 'get_config_manager'))"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.stdout.strip() == "True", out.stderr
    assert list(tmp_path.iterdir()) == []


def test_first_run_defaults(home):
    cm = ConfigManager()
    assert cm.first_run is True
    assert cm.config.index_paths == []
    assert cm.config.config_version == 2
    assert cm.config.exclude_patterns == list(DEFAULT_EXCLUDES)
    assert json.loads(cfg_file().read_text(encoding="utf-8"))["config_version"] == 2
    assert cm.take_notices() == []


def test_migrate_v1_custom_excludes_and_roots(home, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    data = dict(V1_BASE, index_paths=[str(root)], exclude_patterns=["*.tmp", ".git", "build"])
    cfg_file().write_text(json.dumps(data), encoding="utf-8")
    cm = ConfigManager()
    c = cm.config
    assert cm.first_run is False
    assert c.config_version == 2
    assert c.index_paths == [str(root)]
    assert c.exclude_patterns[:3] == ["*.tmp", ".git", "build"]  # user's list first, order kept
    assert all(d in c.exclude_patterns for d in DEFAULT_EXCLUDES)
    assert c.theme == "dark" and c.search_history == ["a", "b"]
    saved = json.loads(cfg_file().read_text(encoding="utf-8"))
    for dropped in ("max_results", "max_index_size", "auto_index_interval"):
        assert dropped not in saved
    assert (cfg_file().parent / "config.v1.json").exists()
    assert cm.take_notices() == []


def test_migrate_v1_home_root_gets_notice_once(home):
    cfg_file().write_text(json.dumps(dict(V1_BASE, index_paths=[str(home)])), encoding="utf-8")
    cm = ConfigManager()
    assert cm.config.index_paths == [str(home)]  # existing users keep their root
    notices = cm.take_notices()
    assert [n.code for n in notices] == ["home_excludes_added"]
    assert "Library, AppData and caches" in notices[0].text
    assert cm.take_notices() == []
    assert ConfigManager().take_notices() == []  # already migrated: no second notice


def test_migrate_v1_empty_roots_stay_empty(home):
    cfg_file().write_text(json.dumps(V1_BASE), encoding="utf-8")
    cm = ConfigManager()
    assert cm.config.index_paths == []
    assert cm.first_run is False


def test_removed_default_exclude_is_respected(home):
    cfg_file().write_text(json.dumps(V1_BASE), encoding="utf-8")
    cm = ConfigManager()
    cm.config.exclude_patterns.remove("node_modules")
    cm.save()
    cm2 = ConfigManager()
    assert "node_modules" not in cm2.config.exclude_patterns
    assert cm2.config.removed_default_excludes == ["node_modules"]
    cm2.config.exclude_patterns.append("node_modules")
    cm2.save()
    assert ConfigManager().config.removed_default_excludes == []


def test_unknown_keys_survive_save(home):
    data = dict(V1_BASE, config_version=2, future_option={"x": [1, 2]}, other=7)
    cfg_file().write_text(json.dumps(data), encoding="utf-8")
    cm = ConfigManager()
    cm.config.theme = "light"
    cm.save()
    saved = json.loads(cfg_file().read_text(encoding="utf-8"))
    assert saved["future_option"] == {"x": [1, 2]} and saved["other"] == 7
    assert saved["theme"] == "light"


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "", "\x00\x01"])
def test_corrupt_file_recovers_with_notice(home, text):
    cfg_file().write_text(text, encoding="utf-8")
    cm = ConfigManager()
    assert cm.config.index_paths == [] and cm.first_run is False
    assert (cfg_file().parent / "config.corrupt.json").read_text(encoding="utf-8") == text
    assert [n.code for n in cm.take_notices()] == ["config_recovered"]
    assert json.loads(cfg_file().read_text(encoding="utf-8"))["config_version"] == 2


def test_validation_coerces_bad_values(tmp_path):
    a = tmp_path / "a"
    c = AppConfig.from_dict({
        "index_paths": [str(a), str(a / "inner"), str(a), 5, ""],
        "exclude_patterns": "nope", "include_home_library": "yes", "index_hidden": 0,
        "periodic_rescan_minutes": 2, "theme": "purple", "search_history": [1, "x"],
    })
    assert c.index_paths == [str(a)]  # duplicate and nested dropped
    assert c.exclude_patterns == []
    assert c.include_home_library is False and c.index_hidden is True
    assert c.periodic_rescan_minutes == 5
    assert c.theme == "light" and c.search_history == ["x"]
    assert AppConfig.from_dict({"periodic_rescan_minutes": 99999}).periodic_rescan_minutes == 1440
    assert AppConfig.from_dict({"periodic_rescan_minutes": True}).periodic_rescan_minutes == 30
    assert AppConfig.from_dict({"periodic_rescan_minutes": "9"}).periodic_rescan_minutes == 30


def test_listener_gets_snapshot(home):
    cm = ConfigManager()
    seen = []
    cm.register_listener(seen.append)
    cm.config.theme = "dark"
    cm.save()
    assert seen[-1].theme == "dark"
    seen[-1].theme = "light"
    assert cm.config.theme == "dark"


def test_get_config_manager_is_lazy_singleton(home):
    cfgmod.reset_config_manager()
    assert cfgmod._manager is None
    a = get_config_manager()
    assert a is get_config_manager()
    assert cfgmod.config_manager is a  # legacy attribute
    cfgmod.reset_config_manager()


def test_build_engine_bootstrap(home, tmp_path):
    root = tmp_path / "r"
    cfg = AppConfig(index_paths=[str(root)], periodic_rescan_minutes=45, index_hidden=False)
    boot = build_engine_bootstrap(cfg)
    assert boot.config.roots == (str(root),)
    assert boot.config.periodic_rescan_minutes == 45 and boot.config.index_hidden is False
    assert set(boot.config.exclude_patterns) >= set(DEFAULT_EXCLUDES)
    for d in (boot.cache_dir, boot.data_dir, boot.log_dir):
        assert Path(d).is_dir()
    assert boot.own_paths and boot.log_level in ("INFO", "DEBUG")
