from __future__ import annotations

import ntpath
import os
import posixpath
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import quote


def _system() -> str:
    """"Windows", "Darwin" or "Linux" (any other POSIX). Read from
    ``sys.platform`` at call time so tests can monkeypatch it."""
    if sys.platform == "win32":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


def _xdg_dir(var: str, fallback: Path) -> Path:
    # The XDG spec says empty or relative values must be ignored.
    value = os.getenv(var, "")
    return Path(value) if value and os.path.isabs(value) else fallback


def _clean_env() -> dict:
    """Environment for launching *external* programs. Frozen builds (PyInstaller,
    Nuitka) point LD_LIBRARY_PATH at bundled libs, which breaks xdg-open/gio."""
    env = dict(os.environ)
    if _system() == "Linux":
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        elif getattr(sys, "frozen", False) or "__compiled__" in globals():
            env.pop("LD_LIBRARY_PATH", None)
    return env


def get_app_data_dir(app_name: Optional[str] = None) -> Path:
    """Per-user config directory: %APPDATA% on Windows, ~/Library/Application
    Support on macOS, $XDG_CONFIG_HOME (~/.config) elsewhere."""
    system = _system()
    home = Path.home()
    if system == "Windows":
        base = Path(os.getenv('APPDATA') or home / 'AppData' / 'Roaming')
    elif system == "Darwin":
        base = home / 'Library' / 'Application Support'
    else:
        # Linux and other UNIX-like systems
        base = _xdg_dir('XDG_CONFIG_HOME', home / '.config')
    path = base / (app_name or 'fast_file_search')
    return path


def _ensure_dir(path: Path, app_name: Optional[str]) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Unwritable/ill-defined root (e.g. bogus LOCALAPPDATA): fall back
        # to a per-user dot-directory so the data still persists.
        path = Path.home() / '.fast_file_search' / (app_name or 'fast_file_search')
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_dir(app_name: Optional[str] = None) -> Path:
    """Persistent per-user cache directory (%LOCALAPPDATA% on Windows,
    ~/Library/Caches on macOS, $XDG_CACHE_HOME (~/.cache) elsewhere).

    ``FAST_FILE_SEARCH_CACHE_DIR`` overrides the base directory (useful for
    portable installs and tests).
    """
    system = _system()
    home = Path.home()
    override = os.getenv('FAST_FILE_SEARCH_CACHE_DIR')
    if override:
        base = Path(override)
    elif system == "Windows":
        base = Path(os.getenv('LOCALAPPDATA') or home / 'AppData' / 'Local')
    elif system == "Darwin":
        base = home / 'Library' / 'Caches'
    else:
        base = _xdg_dir('XDG_CACHE_HOME', home / '.cache')
    return _ensure_dir(base / (app_name or 'fast_file_search'), app_name)


def get_data_dir(app_name: Optional[str] = None) -> Path:
    """Per-user directory for data that must not be purged like a cache
    (history, favorites, tags). Same as :func:`get_cache_dir` on Windows (so
    existing data keeps working) and when ``FAST_FILE_SEARCH_CACHE_DIR`` is
    set; ~/Library/Application Support on macOS; $XDG_DATA_HOME
    (~/.local/share) elsewhere."""
    system = _system()
    if os.getenv('FAST_FILE_SEARCH_CACHE_DIR') or system == "Windows":
        return get_cache_dir(app_name)
    home = Path.home()
    if system == "Darwin":
        base = home / 'Library' / 'Application Support'
    else:
        base = _xdg_dir('XDG_DATA_HOME', home / '.local' / 'share')
    return _ensure_dir(base / (app_name or 'fast_file_search'), app_name)


def get_default_index_root() -> str:
    """Folder indexed when the user has configured none: the home directory
    (never the CWD, which is "/" or inside the .app bundle for GUI launches)."""
    return str(Path.home())


# ---------------------------------------------------------------------- path rules

def paths_case_insensitive() -> bool:
    """Windows (NTFS) and macOS (default APFS/HFS+) compare names ignoring case;
    Linux file systems are case-sensitive."""
    return _system() in ("Windows", "Darwin")


def path_key(path: str | Path) -> str:
    """Comparison key for a path under the platform's rules: normalized
    separators/``..``, case-folded on Windows/macOS and NFC-normalized on macOS
    (APFS is normalization-insensitive, so NFD and NFC name the same file)."""
    s = os.fspath(path)
    system = _system()
    s = ntpath.normpath(s) if system == "Windows" else posixpath.normpath(s)
    if system == "Darwin" and not s.isascii():
        s = unicodedata.normalize("NFC", s)
    return s.casefold() if paths_case_insensitive() else s


def paths_equal(a: str | Path, b: str | Path) -> bool:
    return path_key(a) == path_key(b)


def is_path_under(path: str | Path, root: str | Path) -> bool:
    """True if ``path`` is ``root`` itself or anywhere below it (component-wise,
    so ``/a/bc`` is not under ``/a/b``)."""
    p, r = path_key(path), path_key(root)
    if p == r:
        return True
    sep = "\\" if _system() == "Windows" else "/"
    return p.startswith(r if r.endswith(sep) else r + sep)


def dedupe_roots(paths: Iterable[str]) -> List[str]:
    """Drop duplicate roots (per platform case rules) and roots nested inside
    another root, keeping the first spelling and the original order."""
    result: List[str] = []
    for p in paths:
        if not p or any(is_path_under(p, kept) for kept in result):
            continue
        result = [kept for kept in result if not is_path_under(kept, p)]
        result.append(p)
    return result


# ---------------------------------------------------------------------- desktop integration

def _qt_app():
    """The running QGuiApplication if called on its (GUI) thread, else None."""
    if "PySide6.QtGui" not in sys.modules:
        return None
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            return app
    except Exception:
        pass
    return None


def _file_uri(p: Path) -> str:
    # Percent-encode everything but "/" so quotes, commas and non-UTF-8 bytes
    # can never break out of the D-Bus argument syntax.
    return "file://" + quote(os.fsencode(str(p)), safe="/")


def _freedesktop_show_items(p: Path) -> bool:
    """Ask the file manager to reveal (select) ``p`` via
    org.freedesktop.FileManager1.ShowItems. False if no service answered."""
    uri = _file_uri(p)
    attempts = []
    if shutil.which("gdbus"):
        attempts.append([
            "gdbus", "call", "--session",
            "--dest", "org.freedesktop.FileManager1",
            "--object-path", "/org/freedesktop/FileManager1",
            "--method", "org.freedesktop.FileManager1.ShowItems",
            # GVariant text: an empty string must be written as ''
            f"['{uri}']", "''",
        ])
    if shutil.which("dbus-send"):
        attempts.append([
            "dbus-send", "--session", "--print-reply",
            "--dest=org.freedesktop.FileManager1",
            "--type=method_call", "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1.ShowItems",
            f"array:string:{uri}", "string:",
        ])
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=5, env=_clean_env(), check=False)
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def _launch(cmd: List[str]) -> bool:
    """Start a GUI helper without waiting for it (and without a shell)."""
    try:
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, env=_clean_env())
        if _system() != "Windows":
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        return True
    except (OSError, ValueError):
        return False


def open_file_manager(path: str | Path) -> bool:
    """Reveal ``path`` in the OS file manager (selected in its parent folder).

    Windows: ``explorer /select,``; macOS: ``open -R``; Linux: the
    FileManager1 D-Bus ShowItems call, falling back to ``xdg-open`` on the
    parent folder. Raises FileNotFoundError for a missing path, otherwise
    best-effort: returns False instead of raising.
    """
    p = Path(path)
    if not os.path.lexists(p):
        raise FileNotFoundError(p)
    p = p.absolute()
    system = _system()
    try:
        if system == "Windows":
            # Explorer: select file. Explorer parses its own command line and
            # needs /select,"path" as one token (a list arg would leave a path
            # with a comma but no space unquoted). Windows paths cannot contain
            # '"', and a str runs via CreateProcess without any shell.
            subprocess.run(f'explorer /select,"{p}"', check=False)
            return True
        elif system == "Darwin":
            proc = subprocess.run(['open', '-R', str(p)], check=False)
            return proc.returncode == 0
        else:
            if _freedesktop_show_items(p):
                return True
            # Fallback: open the containing folder
            return _launch(['xdg-open', str(p if p.is_dir() and not p.is_symlink() else p.parent)])
    except Exception:
        # Best-effort; do not raise for UI helper
        return False


def open_path(path: str | Path) -> bool:
    """Open a file/folder with its default application."""
    p = Path(path)
    if not os.path.lexists(p):
        return False
    if _qt_app() is not None:
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.absolute()))):
                return True
        except Exception:
            pass
    system = _system()
    try:
        if system == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
            return True
        if system == "Darwin":
            return _launch(["open", str(p)])
        return _launch(["xdg-open", str(p)])
    except Exception:
        return False


def _trash_windows(p: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(
        wFunc=FO_DELETE,
        pFrom=str(p.absolute()) + "\0",  # list must be double-NUL terminated
        fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI,
    )
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted


def move_to_trash(path: str | Path) -> bool:
    """Move a file/folder to the Recycle Bin / Trash. Returns True on success;
    never deletes permanently."""
    p = Path(path)
    if not os.path.lexists(p):
        return False
    try:
        from PySide6.QtCore import QFile

        result = QFile.moveToTrash(str(p.absolute()))
        ok = result[0] if isinstance(result, tuple) else bool(result)
        if ok and not os.path.lexists(p):
            return True
    except Exception:
        pass
    system = _system()
    try:
        if system == "Windows":
            return _trash_windows(p) and not os.path.lexists(p)
        if system == "Darwin":
            # The path is passed as an argument, never spliced into the script.
            cmds = [["osascript", "-e", "on run argv",
                     "-e", 'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
                     "-e", "end run", str(p.absolute())]]
        else:
            cmds = [["gio", "trash", "--", str(p)], ["trash-put", "--", str(p)],
                    ["kioclient5", "move", str(p), "trash:/"]]
        for cmd in cmds:
            if not shutil.which(cmd[0]):
                continue
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=30, env=_clean_env(), check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if not os.path.lexists(p):
                return True
    except Exception:
        pass
    return False


def copy_to_clipboard(text: str) -> bool:
    app = _qt_app()
    if app is not None:
        try:
            app.clipboard().setText(text)
            return True
        except Exception:
            pass
    system = _system()
    try:
        if system == "Windows":
            # Pass the text through the environment so quotes/$()/backticks in
            # file names can never be interpreted as PowerShell code.
            env = dict(os.environ, FFS_CLIPBOARD_TEXT=text)
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Set-Clipboard -Value $env:FFS_CLIPBOARD_TEXT"],
                check=False,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return proc.returncode == 0
        elif system == "Darwin":
            p = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
            p.communicate(input=text.encode('utf-8'))
            return p.returncode == 0
        else:
            cmds = [['xclip', '-selection', 'clipboard'], ['xsel', '--clipboard', '--input']]
            if os.getenv('WAYLAND_DISPLAY'):
                cmds.insert(0, ['wl-copy'])
            for cmd in cmds:
                try:
                    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=_clean_env())
                    p.communicate(input=text.encode('utf-8', 'surrogateescape'), timeout=5)
                    if p.returncode == 0:
                        return True
                except (OSError, subprocess.SubprocessError):
                    continue
            return False
    except Exception:
        return False


def _qt_color_scheme() -> Optional[str]:
    """Qt 6.5+ color scheme of a running app ("dark"/"light"), None if unknown."""
    if _qt_app() is None:
        return None
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
        if scheme == Qt.ColorScheme.Light:
            return "light"
    except Exception:
        pass
    return None


def _run_text(cmd: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def get_system_theme() -> str:
    """"dark" or "light" for the desktop's current appearance."""
    qt = _qt_color_scheme()
    if qt is not None:
        return qt
    system = _system()
    try:
        if system == "Windows":
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                ) as key:
                    val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if val == 1 else "dark"
            except Exception:
                return "light"
        elif system == "Darwin":
            # The key only exists (value "Dark") in dark mode.
            out = _run_text(["defaults", "read", "-g", "AppleInterfaceStyle"])
            return "dark" if out and out.lower() == "dark" else "light"
        else:
            # GNOME 42+/freedesktop: 'prefer-dark' | 'prefer-light' | 'default'
            out = (_run_text(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]) or "")
            out = out.strip("'\" ").lower()
            if "dark" in out:
                return "dark"
            if "light" in out:
                return "light"
            gtk_theme = os.getenv("GTK_THEME", "")
            if not gtk_theme:
                gtk_theme = (_run_text(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"]) or "")
            return "dark" if "dark" in gtk_theme.lower() else "light"
    except Exception:
        return "light"


def normalize_path(path: str | Path) -> Path:
    p = Path(path)
    try:
        rp = p.expanduser().resolve()
    except Exception:
        rp = p
    # On Windows, normalize drive letter to uppercase
    if _system() == "Windows":
        try:
            drive = rp.drive
            rest = rp.root + rp.relative_to(rp.anchor).as_posix() if rp.anchor else rp.as_posix()
            rp = Path(drive.upper() + rest)
        except Exception:
            pass
    return rp
