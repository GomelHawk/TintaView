"""Electron safeStorage decryption (`stats/providers/_safe_storage.py`)."""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

from tintaview.stats.providers import _safe_storage as ss


def test_split_v10_takes_nonce_and_tag_off_the_ends():
    nonce, ct, tag = ss.split_v10(b"v10" + b"N" * 12 + b"body" + b"T" * 16)
    assert (nonce, ct, tag) == (b"N" * 12, b"body", b"T" * 16)


@pytest.mark.parametrize("blob", [b"v11" + b"x" * 40, b"v10" + b"x" * 27, b""])
def test_split_v10_rejects_other_versions_and_short_blobs(blob):
    with pytest.raises(ss.SafeStorageError):
        ss.split_v10(blob)


def test_wrapped_key_strips_the_dpapi_prefix(tmp_path):
    state = tmp_path / "Local State"
    state.write_text(json.dumps({"os_crypt": {
        "encrypted_key": base64.b64encode(b"DPAPI" + b"blob").decode()}}), encoding="utf-8")
    assert ss.wrapped_key(state) == b"blob"


@pytest.mark.parametrize("content", ["{}", "not json", json.dumps(
    {"os_crypt": {"encrypted_key": base64.b64encode(b"XXXXXblob").decode()}})])
def test_wrapped_key_fails_soft(tmp_path, content):
    state = tmp_path / "Local State"
    state.write_text(content, encoding="utf-8")
    with pytest.raises(ss.SafeStorageError):
        ss.wrapped_key(state)


def test_missing_local_state_is_a_safe_storage_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ss.sys, "platform", "win32")
    with pytest.raises(ss.SafeStorageError):
        ss.decrypt(b"v10" + b"x" * 40, tmp_path / "Local State")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI and CNG are Windows-only")
def test_round_trip_through_real_dpapi_and_cng(tmp_path):
    """Builds exactly what Electron writes — a DPAPI-wrapped key in `Local State` and a
    v10 AES-GCM blob — with the OS's own primitives, then decrypts it."""
    key = os.urandom(32)
    nonce = os.urandom(12)
    state = tmp_path / "Local State"
    state.write_text(json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(
        b"DPAPI" + ss.dpapi_protect(key)).decode()}}), encoding="utf-8")
    sealed = ss.aes_gcm(key, nonce, b"the-token", encrypt=True)
    assert ss.decrypt(b"v10" + nonce + sealed, state) == "the-token"

    tampered = b"v10" + nonce + bytes([sealed[0] ^ 1]) + sealed[1:]
    with pytest.raises(ss.SafeStorageError):
        ss.decrypt(tampered, state)


# --------------------------------------------------------------------------- AES / POSIX


def test_aes_matches_fips_197_and_sp800_38a():
    assert ss._aes.decrypt_block(bytes(range(16)), bytes.fromhex(
        "69c4e0d86a7b0430d8cdb78070b4c55a")) == bytes.fromhex("00112233445566778899aabbccddeeff")
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    ct = bytes.fromhex("7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2")
    assert ss._aes.cbc_decrypt(key, iv, ct, unpad=False) == bytes.fromhex(
        "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")


# Each body was produced by OpenSSL (`openssl enc -aes-128-cbc`, IV = 16 spaces, key =
# PBKDF2-SHA1(password, "saltysalt", iterations, 16)) from the plaintext "the-token" —
# an implementation independent of `_aes`.
PEANUTS_V10 = b"v10" + bytes.fromhex("4caf7163b4b240582627a63e59eaa7ed")
KEYRING_V11 = b"v11" + bytes.fromhex("64de03982a8d6895b0ab8619f3d68320")
MAC_V10 = b"v10" + bytes.fromhex("f87084382518704258a8471a2e945bbf")


def _no_keyring_tools(argv):
    pytest.fail(f"unexpected keyring call {argv[0]}")


def test_linux_v10_uses_the_fixed_peanuts_password(monkeypatch):
    monkeypatch.setattr(ss.sys, "platform", "linux")
    monkeypatch.setattr(ss, "_secret", _no_keyring_tools)
    assert ss.decrypt(PEANUTS_V10, ss.Path("unused")) == "the-token"


def test_linux_v11_asks_libsecret_for_the_apps_password(monkeypatch):
    calls = []

    def fake_secret(argv):
        calls.append(argv)
        return b"keyring-pw" if argv[0] == "secret-tool" and "cursor" in argv else None

    monkeypatch.setattr(ss.sys, "platform", "linux")
    monkeypatch.setattr(ss, "_secret", fake_secret)
    assert ss.decrypt(KEYRING_V11, ss.Path("unused")) == "the-token"
    assert calls[0][:4] == ["secret-tool", "lookup", "xdg:schema",
                            "chrome_libsecret_os_crypt_password_v2"]


def test_linux_v11_falls_back_to_kwallet(monkeypatch):
    monkeypatch.setattr(ss.sys, "platform", "linux")
    monkeypatch.setattr(ss, "_secret",
                        lambda argv: b"keyring-pw" if argv[0] == "kwallet-query" else None)
    assert ss.decrypt(KEYRING_V11, ss.Path("unused")) == "the-token"


def test_linux_v11_without_a_keyring_is_a_safe_storage_error(monkeypatch):
    monkeypatch.setattr(ss.sys, "platform", "linux")
    monkeypatch.setattr(ss, "_secret", lambda argv: None)
    with pytest.raises(ss.SafeStorageError):
        ss.decrypt(KEYRING_V11, ss.Path("unused"))


def test_macos_reads_the_keychain_item_and_uses_1003_iterations(monkeypatch):
    seen = []
    monkeypatch.setattr(ss.sys, "platform", "darwin")
    monkeypatch.setattr(ss, "_secret", lambda argv: seen.append(argv) or b"mac-pw")
    assert ss.decrypt(MAC_V10, ss.Path("unused")) == "the-token"
    assert seen == [["security", "find-generic-password", "-w", "-s", "Cursor Safe Storage"]]


def test_a_wrong_password_is_a_safe_storage_error_not_garbage(monkeypatch):
    monkeypatch.setattr(ss.sys, "platform", "darwin")
    monkeypatch.setattr(ss, "_secret", lambda argv: b"not-the-password")
    with pytest.raises(ss.SafeStorageError):
        ss.decrypt(MAC_V10, ss.Path("unused"))
