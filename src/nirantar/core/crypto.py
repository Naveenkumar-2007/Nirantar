"""Field-level encryption for PII (AES-256-GCM, versioned keys for rotation) and keyed lookup hashes.

Ciphertext layout: b"v1" | key_id(1 byte len + ascii) | 12-byte nonce | ciphertext+tag.
The tenant id is bound as associated data, so a ciphertext copied into another
tenant's row fails to decrypt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nirantar.core.errors import NirantarError


class CryptoError(NirantarError):
    pass


@lru_cache(maxsize=1)
def _keyring() -> tuple[str, dict[str, bytes]]:
    """DATA_KEYS='k2:<b64>,k1:<b64>' (first = active). Local/test get a fixed dev key."""
    raw = os.environ.get("DATA_KEYS", "")
    if not raw:
        if os.environ.get("NIRANTAR_ENV", "local") not in ("local", "test"):
            raise CryptoError("DATA_KEYS must be configured outside local/test")
        return "dev", {"dev": hashlib.sha256(b"nirantar-local-dev-key").digest()}
    keys: dict[str, bytes] = {}
    order: list[str] = []
    for item in raw.split(","):
        kid, _, b64 = item.strip().partition(":")
        key = base64.b64decode(b64)
        if len(key) != 32:
            raise CryptoError(f"key {kid} must be 32 bytes")
        keys[kid] = key
        order.append(kid)
    return order[0], keys


def encrypt(plaintext: str, tenant_id: str) -> bytes:
    active, keys = _keyring()
    nonce = os.urandom(12)
    ct = AESGCM(keys[active]).encrypt(nonce, plaintext.encode(), tenant_id.encode())
    kid = active.encode()
    return b"v1" + bytes([len(kid)]) + kid + nonce + ct


def decrypt(blob: bytes, tenant_id: str) -> str:
    if not blob.startswith(b"v1"):
        raise CryptoError("unknown ciphertext version")
    klen = blob[2]
    kid = blob[3:3 + klen].decode()
    nonce = blob[3 + klen:15 + klen]
    ct = blob[15 + klen:]
    _, keys = _keyring()
    if kid not in keys:
        raise CryptoError(f"key {kid} not available")
    try:
        return AESGCM(keys[kid]).decrypt(nonce, ct, tenant_id.encode()).decode()
    except Exception as exc:
        raise CryptoError("decryption failed") from exc


def lookup_hash(value: str, tenant_id: str) -> str:
    """Keyed, tenant-scoped hash for equality lookups without decrypting (e.g. find customer by phone)."""
    _, keys = _keyring()
    key = hashlib.sha256(b"lookup" + b"".join(sorted(keys.values()))[:32]).digest()
    return hmac.new(key, f"{tenant_id}|{value.strip().lower()}".encode(), hashlib.sha256).hexdigest()
