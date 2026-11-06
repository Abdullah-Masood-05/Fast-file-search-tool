"""Tiny ctypes bridge to Foundation (no PyObjC): App Nap activity assertions and
the Low Power Mode flag (design C.4, G.7). Every function is a safe no-op
(returns None/False) off macOS or if the runtime cannot be loaded."""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
import threading

_log = logging.getLogger(__name__)

# NSActivityUserInitiated (0x00FFFFFF) without NSActivityIdleSystemSleepDisabled (1 << 20)
NSActivityUserInitiatedAllowingIdleSystemSleep = 0x00FFFFFF & ~(1 << 20)

_objc = None
_lock = threading.Lock()
_loaded = False


def _load():
    global _objc, _loaded
    with _lock:
        if _loaded:
            return _objc
        _loaded = True
        if sys.platform != "darwin":
            return None
        try:
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib")
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
            objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
            _objc = objc
        except Exception:
            _log.debug("objc runtime unavailable", exc_info=True)
            _objc = None
        return _objc


def _send(restype, obj, sel: bytes, *args):
    """objc_msgSend with an exact prototype (required on arm64)."""
    objc = _objc
    argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [a[0] for a in args]
    fn = ctypes.CFUNCTYPE(restype, *argtypes)(ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)
    return fn(obj, objc.sel_registerName(sel), *[a[1] for a in args])


def _process_info():
    cls = _objc.objc_getClass(b"NSProcessInfo")
    return _send(ctypes.c_void_p, cls, b"processInfo")


def _nsstring(text: str):
    cls = _objc.objc_getClass(b"NSString")
    return _send(ctypes.c_void_p, cls, b"stringWithUTF8String:", (ctypes.c_char_p, text.encode("utf-8")))


def begin_activity(reason: str = "Indexing files"):
    """Start a user-initiated activity (prevents App Nap). Returns a token for
    end_activity(), or None."""
    if _load() is None:
        return None
    pool = _objc.objc_autoreleasePoolPush()
    try:
        pi = _process_info()
        act = _send(ctypes.c_void_p, pi, b"beginActivityWithOptions:reason:",
                    (ctypes.c_uint64, NSActivityUserInitiatedAllowingIdleSystemSleep),
                    (ctypes.c_void_p, _nsstring(reason)))
        if act:
            _send(ctypes.c_void_p, act, b"retain")
        return act or None
    except Exception:
        _log.debug("beginActivity failed", exc_info=True)
        return None
    finally:
        _objc.objc_autoreleasePoolPop(pool)


def end_activity(token) -> None:
    if not token or _load() is None:
        return
    pool = _objc.objc_autoreleasePoolPush()
    try:
        _send(None, _process_info(), b"endActivity:", (ctypes.c_void_p, token))
        _send(None, token, b"release")
    except Exception:
        _log.debug("endActivity failed", exc_info=True)
    finally:
        _objc.objc_autoreleasePoolPop(pool)


def low_power_mode() -> bool:
    """NSProcessInfo.isLowPowerModeEnabled (macOS 12+); False if unknown."""
    if _load() is None:
        return False
    pool = _objc.objc_autoreleasePoolPush()
    try:
        pi = _process_info()
        return bool(_send(ctypes.c_byte, pi, b"isLowPowerModeEnabled"))
    except Exception:
        return False
    finally:
        _objc.objc_autoreleasePoolPop(pool)


class ActivityAssertion:
    """Holds one activity while any scan runs (idempotent begin/end)."""

    def __init__(self):
        self._token = None

    def set_active(self, active: bool) -> None:
        if active and self._token is None:
            self._token = begin_activity()
        elif not active and self._token is not None:
            end_activity(self._token)
            self._token = None
