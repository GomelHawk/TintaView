"""AES-128 decryption (FIPS-197) and CBC mode (SP 800-38A), pure Python, decrypt only.

Exists for one job: unwrapping Electron ``safeStorage`` values on Linux and macOS,
where Chromium's ``os_crypt`` uses AES-128-CBC and no OS crypto API is reachable
from the standard library (Windows uses CNG instead — see ``_safe_storage``). The
input is a few hundred bytes per poll, so speed is irrelevant; correctness is pinned
by the published test vectors in ``tests/test_safe_storage.py``.
"""

from __future__ import annotations


def _xtime(b: int) -> int:
    b <<= 1
    return (b ^ 0x11B) if b & 0x100 else b


def _mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _build_sbox() -> tuple[list[int], list[int]]:
    # Multiplicative inverses in GF(2^8) from powers of the generator 3 (0 maps to 0),
    # then the affine transform.
    powers = [1] * 255
    for i in range(1, 255):
        powers[i] = _mul(powers[i - 1], 3)
    inverse = [0] * 256
    for i, p in enumerate(powers):
        inverse[p] = powers[(255 - i) % 255]
    sbox = [0] * 256
    for x in range(256):
        inv = inverse[x]
        s = inv
        for shift in range(1, 5):
            s ^= ((inv << shift) | (inv >> (8 - shift))) & 0xFF
        sbox[x] = s ^ 0x63
    inv_sbox = [0] * 256
    for i, s in enumerate(sbox):
        inv_sbox[s] = i
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_sbox()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _expand_key(key: bytes) -> list[list[int]]:
    """Eleven 16-byte round keys for a 128-bit key."""
    if len(key) != 16:
        raise ValueError("AES-128 needs a 16-byte key")
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([a ^ b for a, b in zip(words[i - 4], temp, strict=True)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(11)]


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    # State is column-major: state[row + 4 * col], the same order as the input bytes.
    state = [b ^ k for b, k in zip(block, round_keys[10], strict=True)]
    for rnd in range(9, -1, -1):
        # InvShiftRows: row r rotates right by r.
        state = [state[(r + 4 * ((c - r) % 4))] for c in range(4) for r in range(4)]
        state = [_INV_SBOX[b] for b in state]
        state = [b ^ k for b, k in zip(state, round_keys[rnd], strict=True)]
        if rnd:
            mixed = []
            for c in range(4):
                a0, a1, a2, a3 = state[4 * c:4 * c + 4]
                mixed += [
                    _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9),
                    _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13),
                    _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11),
                    _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14),
                ]
            state = mixed
    return bytes(state)


def decrypt_block(key: bytes, block: bytes) -> bytes:
    """One raw AES-128 block — exposed for the FIPS-197 test vector."""
    return _decrypt_block(block, _expand_key(key))


def cbc_decrypt(key: bytes, iv: bytes, data: bytes, *, unpad: bool = True) -> bytes:
    """AES-128-CBC decrypt; strips and checks PKCS#7 padding unless ``unpad=False``."""
    if len(iv) != 16 or not data or len(data) % 16:
        raise ValueError("CBC input must be whole 16-byte blocks with a 16-byte IV")
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, round_keys), prev, strict=True))
        prev = block
    if not unpad:
        return bytes(out)
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad PKCS#7 padding (wrong key?)")
    return bytes(out[:-pad])
