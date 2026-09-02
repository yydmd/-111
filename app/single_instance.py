from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes


ERROR_ALREADY_EXISTS = 183
_mutex_handle: wintypes.HANDLE | None = None


def acquire_service_mutex() -> bool:
    """Allow one local web service per Windows user without storing user data in the name."""
    global _mutex_handle
    if _mutex_handle:
        return True
    identity = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\ChaoXingReserveSeat-{suffix}")
    if not _mutex_handle:
        raise ctypes.WinError()
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
        return False
    return True


def release_service_mutex() -> None:
    global _mutex_handle
    if _mutex_handle:
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
