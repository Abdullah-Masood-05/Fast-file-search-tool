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
    "FAST_FILE_SEARCH_CACHE_DIR": _SANDBOX / "cache",
}
for _name, _path in _ENV.items():
    _path.mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_path)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
