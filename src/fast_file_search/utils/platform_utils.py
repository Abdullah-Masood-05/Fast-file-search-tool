from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Optional


def get_app_data_dir(app_name: Optional[str] = None) -> Path:
    system = platform.system()
    home = Path.home()
    if system == "Windows":
        base = Path(os.getenv('APPDATA', home / 'AppData' / 'Roaming'))
    elif system == "Darwin":
        base = home / 'Library' / 'Application Support'
    else:
        # Linux and other UNIX-like systems
        base = Path(os.getenv('XDG_CONFIG_HOME', home / '.config'))
    path = base / (app_name or 'fast_file_search')
    return path


def get_cache_dir(app_name: Optional[str] = None) -> Path:
    system = platform.system()
    home = Path.home()
    if system == "Windows":
        base = Path(os.getenv('LOCALAPPDATA', home / 'AppData' / 'Local'))
    elif system == "Darwin":
        base = home / 'Library' / 'Caches'
    else:
        base = Path(os.getenv('XDG_CACHE_HOME', home / '.cache'))
    path = base / (app_name or 'fast_file_search')
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_file_manager(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    system = platform.system()
    try:
        if system == "Windows":
            # Explorer: select file
            subprocess.run(['explorer', '/select,', str(p)], check=False)
        elif system == "Darwin":
            subprocess.run(['open', '-R', str(p)], check=False)
        else:
            # Fallback: open containing folder
            if p.is_dir():
                subprocess.run(['xdg-open', str(p)], check=False)
            else:
                subprocess.run(['xdg-open', str(p.parent)], check=False)
    except Exception:
        # Best-effort; do not raise for UI helper
        pass


def copy_to_clipboard(text: str) -> bool:
    system = platform.system()
    try:
        if system == "Windows":
            # Use powershell Set-Clipboard if available
            subprocess.run(["powershell", "-Command", f"Set-Clipboard -Value \"{text}\""], check=False)
            return True
        elif system == "Darwin":
            p = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
            p.communicate(input=text.encode('utf-8'))
            return p.returncode == 0
        else:
            for cmd in (['wl-copy'], ['xclip', '-selection', 'clipboard']):
                try:
                    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    p.communicate(input=text.encode('utf-8'))
                    if p.returncode == 0:
                        return True
                except FileNotFoundError:
                    continue
            return False
    except Exception:
        return False


def get_system_theme() -> str:
    system = platform.system()
    try:
        if system == "Windows":
            try:
                import winreg

                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize")
                val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if val == 1 else "dark"
            except Exception:
                return "light"
        elif system == "Darwin":
            proc = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
            return "dark" if proc.returncode == 0 and proc.stdout.strip() == "Dark" else "light"
        else:
            # Try GNOME settings
            try:
                proc = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"], capture_output=True, text=True)
                out = proc.stdout.strip().strip("'\" ")
                if out and "dark" in out.lower():
                    return "dark"
            except Exception:
                pass
            return "light"
    except Exception:
        return "light"


def normalize_path(path: str | Path) -> Path:
    p = Path(path)
    try:
        rp = p.expanduser().resolve()
    except Exception:
        rp = p
    # On Windows, normalize drive letter to uppercase
    if platform.system() == "Windows":
        try:
            drive = rp.drive
            rest = rp.root + rp.relative_to(rp.anchor).as_posix() if rp.anchor else rp.as_posix()
            rp = Path(drive.upper() + rest)
        except Exception:
            pass
    return rp
