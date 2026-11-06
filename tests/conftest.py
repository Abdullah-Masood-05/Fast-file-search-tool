"""Keep the test run away from the developer's real config, index and Qt settings.

The app builds its config manager at import time and the UI tests construct a real
MainWindow, so the redirects below must be in place before any test module imports
``fast_file_search``. They are set at conftest import time, which pytest does first.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="ffs-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

_ENV = {
    "HOME": _SANDBOX / "home",
    "USERPROFILE": _SANDBOX / "home",
    "APPDATA": _SANDBOX / "appdata",
    "LOCALAPPDATA": _SANDBOX / "localappdata",
    "XDG_CONFIG_HOME": _SANDBOX / "xdg-config",
    "XDG_CACHE_HOME": _SANDBOX / "xdg-cache",
    "XDG_DATA_HOME": _SANDBOX / "xdg-data",
    "XDG_STATE_HOME": _SANDBOX / "xdg-state",
    # The log, state and cache dirs all derive from this one (platform_utils.get_*_dir).
    "FAST_FILE_SEARCH_CACHE_DIR": _SANDBOX / "cache",
}
for _name, _path in _ENV.items():
    _path.mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_path)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# --------------------------------------------------------------------------- slow tests
# Tests marked ``slow`` spawn real processes or run for many seconds. They are skipped
# unless ``--run-slow`` is given (CI runs them in a dedicated step).

def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true", default=False,
                     help="also run tests marked 'slow' (real processes, long runs)")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: spawns real processes or runs long (needs --run-slow)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    import pytest
    skip = pytest.mark.skip(reason="slow test: use --run-slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
