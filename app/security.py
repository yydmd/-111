from __future__ import annotations

import ctypes
import base64
import binascii
import os
import re
import sys
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("Windows DPAPI is required for credential storage")
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    src = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte)))
    dst = DATA_BLOB()
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel.LocalFree(dst.pbData)


def encrypt_password(password: str) -> bytes:
    return _dpapi(password.encode("utf-8"), True)


def decrypt_password(blob: bytes) -> str:
    return _dpapi(blob, False).decode("utf-8")


_SECRET_PREFIX = "dpapi:v1:"


def protect_secret(value: str) -> str:
    """Store a small local integration secret using the current user's DPAPI."""
    try:
        encrypted = encrypt_password(value)
    except (OSError, RuntimeError) as exc:
        raise ValueError("通知密钥无法加密，请检查当前 Windows 用户环境") from exc
    return _SECRET_PREFIX + base64.urlsafe_b64encode(encrypted).decode("ascii")


def unprotect_secret(value: str) -> str:
    """Read a DPAPI secret, accepting pre-v9 plaintext values for migration."""
    raw = str(value or "")
    if not raw.startswith(_SECRET_PREFIX):
        return raw
    try:
        payload = base64.b64decode(raw[len(_SECRET_PREFIX):].encode("ascii"), altchars=b"-_", validate=True)
        return decrypt_password(payload)
    except (OSError, RuntimeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("通知密钥无法解密，请重新填写并保存") from exc


def redact(value: str) -> str:
    value = re.sub(r"(?<![\w])([\"']?(?:password|passwd|captcha|token|cookie|enc)[\"']?\s*[:=]\s*[\"']?)[^&,\s}\"']+", r"\1<redacted>", value, flags=re.I)
    return value[:2000]
