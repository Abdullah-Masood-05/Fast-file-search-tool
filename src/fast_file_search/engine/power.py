"""Power state (design G.7): on battery / battery saver / Low Power Mode.

``power_state()`` returns a ``PowerState``; the engine polls it every 60 s
(``PowerMonitor``) and throttles background work when ``low_power`` is True.
Each probe fails safe to "on AC" (never throttles on an unknown machine).
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import threading
from typing import Callable, NamedTuple, Optional


class PowerState(NamedTuple):
    on_battery: bool
    saver: bool               # Windows battery saver / macOS Low Power Mode / Linux power-saver profile

    @property
    def low_power(self) -> bool:
        return self.on_battery or self.saver


AC = PowerState(False, False)


def _windows() -> PowerState:
    import ctypes
    from ctypes import wintypes

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                    ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

    s = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return AC
    ac = s.ACLineStatus & 0xFF
    battery_flag = s.BatteryFlag & 0xFF
    no_battery = battery_flag == 128 or battery_flag == 255
    return PowerState(on_battery=(ac == 0 and not no_battery), saver=bool(s.SystemStatusFlag & 1))


def parse_pmset(text: str) -> bool:
    """True when ``pmset -g batt`` says the Mac draws from its battery."""
    lines = text.strip().splitlines()
    return bool(lines) and "Battery Power" in lines[0]   # "Now drawing from 'Battery Power'"


def _macos() -> PowerState:
    on_batt = False
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
        on_batt = parse_pmset(out)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        from . import macos
        saver = macos.low_power_mode()
    except Exception:
        saver = False
    return PowerState(on_batt, saver)


def linux_on_battery(base: str = "/sys/class/power_supply") -> bool:
    """On battery if a Mains supply exists and none is online."""
    mains = []
    for d in glob.glob(os.path.join(base, "*")):
        try:
            with open(os.path.join(d, "type")) as fh:
                kind = fh.read().strip()
            if kind != "Mains":
                continue
            with open(os.path.join(d, "online")) as fh:
                mains.append(fh.read().strip() == "1")
        except OSError:
            continue
    return bool(mains) and not any(mains)


def _linux() -> PowerState:
    saver = False
    exe = shutil.which("powerprofilesctl")
    if exe:
        try:
            out = subprocess.run([exe, "get"], capture_output=True, text=True, timeout=3).stdout
            saver = out.strip() == "power-saver"
        except (OSError, subprocess.SubprocessError):
            pass
    return PowerState(linux_on_battery(), saver)


def power_state(platform: Optional[str] = None) -> PowerState:
    p = sys.platform if platform is None else platform
    override = os.environ.get("FFS_POWER")       # tests / diagnostics: "battery" | "ac" | "saver"
    if override:
        return PowerState(override == "battery", override == "saver")
    try:
        if p == "win32":
            return _windows()
        if p == "darwin":
            return _macos()
        return _linux()
    except Exception:
        return AC


class PowerMonitor:
    """Polls ``probe`` every ``interval`` s and calls ``on_change(state)`` on changes."""

    def __init__(self, on_change: Callable[[PowerState], None], *, interval: float = 60.0,
                 probe: Callable[[], PowerState] = power_state):
        self._on_change = on_change
        self._interval = interval
        self._probe = probe
        self._stop = threading.Event()
        self.state = probe()
        self._thread = threading.Thread(target=self._run, name="ffs-power", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                s = self._probe()
            except Exception:
                continue
            if s != self.state:
                self.state = s
                try:
                    self._on_change(s)
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()
