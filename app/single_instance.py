from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes


ERROR_ALREADY_EXISTS = 183
_mutex_handle: wintypes.HANDLE | None = None


def mutex_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def acquire_service_mutex() -> bool:
    """Allow one local web service per Windows user without storing user data in the name."""
    global _mutex_handle
    if _mutex_handle:
        return True
    identity = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    kernel = mutex_api()
    _mutex_handle = kernel.CreateMutexW(None, False, f"Local\\ChaoXingReserveSeat-{suffix}")
    if not _mutex_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel.CloseHandle(_mutex_handle)
        _mutex_handle = None
        return False
    return True


def release_service_mutex() -> None:
    global _mutex_handle
    if _mutex_handle:
        mutex_api().CloseHandle(_mutex_handle)
        _mutex_handle = None
