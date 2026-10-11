"""File operations triggered from the UI (open, open with, reveal, trash).

The UI calls only this module for OS actions, so tests can replace it with fakes.
Functions return a plain-language error message, or ``None`` on success.
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QFile, QMimeDatabase

from ..utils import platform_utils


def _name(path: str) -> str:
    return Path(path).name or path


def _missing(path: str) -> Optional[str]:
    if not os.path.lexists(path):
        return f'"{_name(path)}" no longer exists. It may have been moved or deleted.'
    return None


def open_path(path: str) -> Optional[str]:
    err = _missing(path)
    if err:
        return err
    if not platform_utils.open_path(path):
        return f'Could not open "{_name(path)}". No app is set up to open this type of file.'
    return None


def reveal(path: str) -> Optional[str]:
    err = _missing(path)
    if err:
        return err
    try:
        ok = platform_utils.open_file_manager(path)
    except FileNotFoundError:
        return _missing(path) or f'"{_name(path)}" no longer exists.'
    except Exception as exc:  # pragma: no cover - defensive
        return f"Could not open the file manager: {exc}"
    if ok is False:
        return "Could not open the file manager."
    return None


# ---------------------------------------------------------------------- open with
def open_with_mode() -> str:
    """How "Open with" works here: ``"system"`` (Windows chooser), ``"choose-app"``
    (macOS: pick an .app), ``"list"`` (Linux: registered apps via gio) or ``""``."""
    if sys.platform == "win32":
        return "system"
    if sys.platform == "darwin":
        return "choose-app"
    return "list" if shutil.which("gio") else ""


def _desktop_file(app_id: str) -> Optional[Path]:
    dirs = [os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")]
    dirs += (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    for d in dirs:
        if not d:
            continue
        # Desktop file IDs map "-" in the ID to "/" subfolders as well.
        for candidate in (Path(d) / "applications" / app_id,
                          Path(d) / "applications" / app_id.replace("-", "/", 1)):
            if candidate.is_file():
                return candidate
    return None


def _desktop_name(path: Path) -> Optional[str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
        entry = parser["Desktop Entry"]
    except (configparser.Error, KeyError, OSError, UnicodeDecodeError):
        return None
    if entry.get("NoDisplay", "false").lower() == "true":
        return None
    return entry.get("Name")


def open_with_choices(path: str) -> List[Tuple[str, str]]:
    """Linux: ``[(app name, desktop file path)]`` registered for the file's type."""
    if open_with_mode() != "list":
        return []
    mime = QMimeDatabase().mimeTypeForFile(path).name()
    try:
        out = subprocess.run(["gio", "mime", mime], capture_output=True, text=True,
                             timeout=3, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    choices: List[Tuple[str, str]] = []
    seen = set()
    for line in out.splitlines():
        app_id = line.strip()
        if not line.startswith(("\t", " ")) or not app_id.endswith(".desktop") or app_id in seen:
            continue
        seen.add(app_id)
        desktop = _desktop_file(app_id)
        name = _desktop_name(desktop) if desktop else None
        if desktop and name:
            choices.append((name, str(desktop)))
    return choices


def open_with(path: str, application: Optional[str] = None) -> Optional[str]:
    """Windows: show the system "Open with" chooser.  macOS: open with the .app
    ``application``.  Linux: launch the desktop file ``application``."""
    err = _missing(path)
    if err:
        return err
    try:
        if sys.platform == "win32":
            subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", os.path.normpath(path)])
        elif not application:
            return None
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", application, path])
        else:
            subprocess.Popen(["gio", "launch", application, path], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return f"Could not start the app: {exc.strerror or exc}"
    return None


# ---------------------------------------------------------------------- delete
def move_to_trash(path: str) -> Tuple[bool, Optional[str]]:
    """Move to the Recycle Bin/Trash.  Returns ``(moved, error)``.

    ``(False, None)`` means this location has no trash; the caller may then offer
    a permanent delete.
    """
    err = _missing(path)
    if err:
        return False, err
    helper = getattr(platform_utils, "move_to_trash", None)
    try:
        if helper is not None:
            ok = bool(helper(path))
        else:  # pragma: no cover - older core module
            ok = bool(QFile.moveToTrash(path)[0])
    except Exception:
        ok = False
    return (True, None) if ok else (False, None)


def delete_permanently(path: str) -> Optional[str]:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError as exc:
        return f'Could not delete "{_name(path)}": {exc.strerror or exc}'
    return None
