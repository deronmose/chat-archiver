"""Encrypted storage for API credentials and the Telegram session.

On Windows the data is encrypted with DPAPI (CryptProtectData): only the same
Windows user on the same computer can decrypt it, so a copied file is useless.
Nothing secret is ever written to disk in plain text. On other systems the file
falls back to owner-only permissions (0600).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ENTROPY = b"tg-export/vault/v1"


def app_home(app_name: str, env_override: str | None = None) -> Path:
    base = os.environ.get(env_override) if env_override else None
    if base:
        return Path(base)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / app_name
    return Path.home() / ".config" / app_name


def home() -> Path:
    return app_home("tg-export", "TG_EXPORT_HOME")


def vault_path() -> Path:
    return home() / "vault.bin"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _UI_FORBIDDEN = 0x1

    def _blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _take(blob: _Blob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def protect(data: bytes) -> bytes:
        inp, _keep1 = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(inp), "tg-export", ctypes.byref(ent),
                                         None, None, _UI_FORBIDDEN, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _take(out)

    def unprotect(data: bytes) -> bytes:
        inp, _keep1 = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent),
                                           None, None, _UI_FORBIDDEN, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _take(out)

    ENCRYPTED = True
else:
    def protect(data: bytes) -> bytes:
        return data

    def unprotect(data: bytes) -> bytes:
        return data

    ENCRYPTED = False


class Vault:
    """One encrypted JSON document at a fixed path."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(unprotect(self.path.read_bytes()).decode("utf-8"))

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = protect(json.dumps(data).encode("utf-8"))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, self.path)

    def delete(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False


# Module-level functions keep the CLI's single default vault.
def load() -> dict:
    return Vault(vault_path()).load()


def save(data: dict) -> None:
    Vault(vault_path()).save(data)


def delete() -> bool:
    return Vault(vault_path()).delete()
