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


_FAULT_FD = None


def pytest_configure(config):
    global _FAULT_FD
    config.addinivalue_line("markers", "slow: spawns real processes or runs long (needs --run-slow)")
    # applog.setup_logging() redirects faulthandler into a crash log file; a native crash
    # in a test would then die silently in CI. Keep our own dup of the real stderr (taken
    # before pytest's capture swaps fd 2) and re-arm faulthandler on it around every test.
    import faulthandler
    try:
        _FAULT_FD = os.dup(2)
        faulthandler.enable(_FAULT_FD, all_threads=True)
    except Exception:
        _FAULT_FD = None
        return
    # Also swallow later redirects (applog.setup_logging called inside a test): the crash
    # dump must reach the CI log, not a file in the sandbox that is deleted at exit.
    real_enable = faulthandler.enable

    def enable(file=None, all_threads=True):
        real_enable(_FAULT_FD, all_threads=all_threads)

    faulthandler.enable = enable


def _rearm_faulthandler():
    if _FAULT_FD is not None:
        import faulthandler
        faulthandler.enable(_FAULT_FD, all_threads=True)


import pytest  # noqa: E402


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _rearm_faulthandler()
    try:
        return (yield)
    finally:
        _rearm_faulthandler()


@pytest.hookimpl(wrapper=True, tryfirst=False)
def pytest_runtest_call(item):
    _rearm_faulthandler()
    return (yield)


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    import pytest
    skip = pytest.mark.skip(reason="slow test: use --run-slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


# --------------------------------------------------------------------------- timing
@pytest.fixture(scope="session")
def perf_scale():
    """Multiplier for absolute latency limits: strict (1.0) locally, looser on shared CI
    runners where neighbouring jobs steal CPU. Relative-to-baseline checks use their own bound."""
    return 3.0 if os.environ.get("CI") else 1.0
