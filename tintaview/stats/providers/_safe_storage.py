"""Decrypt a value Electron's ``safeStorage.encryptString`` wrote, on Windows only.

Cursor (measured, ``cursorAuthTokenStorageCodec.js``) stores
``cursorAuth/accessToken`` as ``"cursor-safe-storage:v1:" + base64(encryptString(token))``
once ``cursor.storage.authTokenEncryptionAtRest`` is on. On Windows that ciphertext is
Chromium's ``os_crypt`` format:

    b"v10" + nonce (12 bytes) + AES-256-GCM ciphertext + tag (16 bytes)

and the AES key is ``os_crypt.encrypted_key`` in the app's ``Local State`` JSON:
base64 of ``b"DPAPI"`` + a DPAPI blob bound to the current Windows user.

Both primitives come from Windows itself through ``ctypes`` (``crypt32`` for DPAPI,
``bcrypt`` for AES-GCM): the stats code runs in a ``--headless`` stdlib-only install
too, and the standard library has no AES. Linux and macOS use AES-128-CBC with a key
from the OS keyring (see the POSIX section below); that AES is ``_aes``, in pure Python.
Anything that can't be decrypted raises ``SafeStorageError`` for the caller to report.

The decrypted value is a live credential: return it, never log it.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from . import _aes

PREFIX_V10 = b"v10"
NONCE_LEN = 12
TAG_LEN = 16
DPAPI_PREFIX = b"DPAPI"


class SafeStorageError(Exception):
    """The value could not be decrypted on this machine."""


def split_v10(blob: bytes) -> tuple[bytes, bytes, bytes]:
    """``(nonce, ciphertext, tag)`` of an ``os_crypt`` v10 blob."""
    if not blob.startswith(PREFIX_V10):
        raise SafeStorageError(f"unsupported safeStorage version {blob[:3]!r}")
    body = blob[len(PREFIX_V10):]
    if len(body) < NONCE_LEN + TAG_LEN:
        raise SafeStorageError("safeStorage blob is too short")
    return body[:NONCE_LEN], body[NONCE_LEN:-TAG_LEN], body[-TAG_LEN:]


def wrapped_key(local_state: Path) -> bytes:
    """The DPAPI blob holding the AES key, from ``Local State``."""
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        encoded = data["os_crypt"]["encrypted_key"]
        raw = base64.b64decode(encoded)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SafeStorageError(f"no os_crypt key in {local_state}: {e!r}") from e
    if not raw.startswith(DPAPI_PREFIX):
        raise SafeStorageError("os_crypt key is not DPAPI-protected")
    return raw[len(DPAPI_PREFIX):]


def decrypt(blob: bytes, local_state: Path, *, app: str = "Cursor") -> str:
    """Plaintext of a ``safeStorage`` blob written by the Electron app ``app``.

    ``local_state`` is only read on Windows; Linux and macOS keep the key in the OS
    keyring under the app's name."""
    if sys.platform == "win32":
        return _decrypt_windows(blob, local_state)
    return _decrypt_cbc(blob, app, darwin=sys.platform == "darwin")


# --------------------------------------------------------------------------- Linux / macOS
#
# Chromium's os_crypt on POSIX: key = PBKDF2-HMAC-SHA1(password, b"saltysalt",
# iterations, 16 bytes); value = prefix + AES-128-CBC(key, IV = 16 spaces, PKCS#7).
#   macOS  "v10": password from the Keychain item "<App> Safe Storage", 1003 iterations.
#   Linux  "v11": password from libsecret (GNOME) or KWallet, 1 iteration.
#   Linux  "v10": the hard-coded password "peanuts" (no keyring), 1 iteration.
# Cursor only encrypts on Linux when the backend is libsecret or KWallet
# (`cursorAuthTokenStorageCodec.js`: `nH`/`tH`); otherwise the token stays plain text.

SALT = b"saltysalt"
IV = b" " * 16
KEYRING_TIMEOUT_S = 15.0  # macOS may show its "allow access" dialog the first time


def derive_key(password: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, SALT, iterations, 16)


def cbc_plaintext(blob: bytes, password: bytes, iterations: int) -> str:
    """Decrypt a POSIX ``os_crypt`` blob (``v10``/``v11`` + CBC body) with ``password``."""
    if blob[:3] not in (b"v10", b"v11"):
        raise SafeStorageError(f"unsupported safeStorage version {blob[:3]!r}")
    try:
        plain = _aes.cbc_decrypt(derive_key(password, iterations), IV, blob[3:])
        return plain.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise SafeStorageError(f"could not decrypt safeStorage value: {e}") from e


def _decrypt_cbc(blob: bytes, app: str, *, darwin: bool) -> str:
    version = blob[:3]
    if darwin:
        return cbc_plaintext(blob, _keychain_password(app), 1003)
    if version == b"v10":
        return cbc_plaintext(blob, b"peanuts", 1)
    return cbc_plaintext(blob, _linux_keyring_password(app), 1)


def _secret(argv: list[str]) -> bytes | None:
    """stdout of a keyring CLI, or None when it is missing, fails or finds nothing.
    The output is a password: never log it."""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=KEYRING_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.rstrip(b"\r\n")
    return value if done.returncode == 0 and value else None


def _keychain_password(app: str) -> bytes:
    found = _secret(["security", "find-generic-password", "-w", "-s", f"{app} Safe Storage"])
    if found is None:
        raise SafeStorageError(f'no "{app} Safe Storage" item in the macOS Keychain')
    return found


def _linux_keyring_password(app: str) -> bytes:
    for name in dict.fromkeys((app.lower(), app)):
        for schema in ("chrome_libsecret_os_crypt_password_v2",
                       "chrome_libsecret_os_crypt_password_v1"):
            found = _secret(["secret-tool", "lookup", "xdg:schema", schema, "application", name])
            if found is not None:
                return found
    # Electron files the key under Chromium's names, not the app's: measured on Linux
    # Cursor 3.27.22 the libsecret item is labelled "Chromium Safe Storage" with
    # `application = Cursor`. KWallet is keyed by name only, so try both spellings.
    for owner in ("Chromium", app):
        found = _secret(["kwallet-query", "-r", f"{owner} Safe Storage", "-f", f"{owner} Keys",
                         "kdewallet"])
        if found is not None:
            return found
    raise SafeStorageError(f"no {app} safeStorage password in libsecret or KWallet "
                           "(is secret-tool / kwallet-query installed?)")


# --------------------------------------------------------------------------- Windows


def _decrypt_windows(blob: bytes, local_state: Path) -> str:
    nonce, ciphertext, tag = split_v10(blob)
    key = dpapi_unprotect(wrapped_key(local_state))
    if len(key) != 32:
        raise SafeStorageError(f"unexpected os_crypt key length {len(key)}")
    plain = aes_gcm(key, nonce, ciphertext, tag, encrypt=False)
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SafeStorageError("decrypted value is not UTF-8") from e


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_in(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    blob_in, _keep = _blob_in(data)
    blob_out = _DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                      ctypes.byref(blob_out)):
        raise SafeStorageError(f"CryptUnprotectData failed ({ctypes.get_last_error()})")  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_protect(data: bytes) -> bytes:
    """Test helper: the inverse of `dpapi_unprotect`, for a real round trip on Windows."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    blob_in, _keep = _blob_in(data)
    blob_out = _DataBlob()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                    ctypes.byref(blob_out)):
        raise SafeStorageError(f"CryptProtectData failed ({ctypes.get_last_error()})")  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class _AuthInfo(ctypes.Structure):
    """BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO."""

    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("dwInfoVersion", ctypes.c_uint32),
        ("pbNonce", ctypes.c_void_p),
        ("cbNonce", ctypes.c_uint32),
        ("pbAuthData", ctypes.c_void_p),
        ("cbAuthData", ctypes.c_uint32),
        ("pbTag", ctypes.c_void_p),
        ("cbTag", ctypes.c_uint32),
        ("pbMacContext", ctypes.c_void_p),
        ("cbMacContext", ctypes.c_uint32),
        ("cbAAD", ctypes.c_uint32),
        ("cbData", ctypes.c_uint64),
        ("dwFlags", ctypes.c_uint32),
    ]


def _check(status: int, what: str) -> None:
    if status != 0:
        raise SafeStorageError(f"{what} failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")


def aes_gcm(key: bytes, nonce: bytes, data: bytes, tag: bytes | None = None, *,
            encrypt: bool) -> bytes:
    """AES-GCM through Windows CNG. Decrypt verifies ``tag``; encrypt (a test helper)
    returns ``ciphertext + tag``."""
    bcrypt = ctypes.WinDLL("bcrypt")  # type: ignore[attr-defined]
    for fn in ("BCryptOpenAlgorithmProvider", "BCryptSetProperty", "BCryptGenerateSymmetricKey",
               "BCryptDecrypt", "BCryptEncrypt", "BCryptDestroyKey",
               "BCryptCloseAlgorithmProvider"):
        getattr(bcrypt, fn).restype = ctypes.c_long
    alg = ctypes.c_void_p()
    hkey = ctypes.c_void_p()
    _check(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), ctypes.c_wchar_p("AES"),
                                              None, 0), "BCryptOpenAlgorithmProvider")
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        _check(bcrypt.BCryptSetProperty(alg, ctypes.c_wchar_p("ChainingMode"), mode,
                                        ctypes.sizeof(mode), 0), "BCryptSetProperty")
        key_buf = ctypes.create_string_buffer(key, len(key))
        _check(bcrypt.BCryptGenerateSymmetricKey(alg, ctypes.byref(hkey), None, 0, key_buf,
                                                 len(key), 0), "BCryptGenerateSymmetricKey")
        try:
            nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
            tag_buf = ctypes.create_string_buffer(tag or b"\0" * TAG_LEN, TAG_LEN)
            info = _AuthInfo()
            info.cbSize = ctypes.sizeof(_AuthInfo)
            info.dwInfoVersion = 1
            info.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
            info.cbNonce = len(nonce)
            info.pbTag = ctypes.cast(tag_buf, ctypes.c_void_p)
            info.cbTag = TAG_LEN
            in_buf = ctypes.create_string_buffer(data, len(data))
            out_buf = ctypes.create_string_buffer(max(len(data), 1))
            done = ctypes.c_uint32(0)
            fn = bcrypt.BCryptEncrypt if encrypt else bcrypt.BCryptDecrypt
            _check(fn(hkey, in_buf, len(data), ctypes.byref(info), None, 0, out_buf, len(data),
                      ctypes.byref(done), 0), "BCryptEncrypt" if encrypt else "BCryptDecrypt")
            out = out_buf.raw[:done.value]
            return out + tag_buf.raw if encrypt else out
        finally:
            bcrypt.BCryptDestroyKey(hkey)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(alg, 0)
