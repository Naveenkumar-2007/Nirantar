"""Time-sortable, prefixed identifiers (ULID layout: 48-bit ms time + 80 random bits)."""

from __future__ import annotations

import re
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9]{1,15}_[0-9A-HJKMNP-TV-Z]{26}$")


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str, *, now_ms: int | None = None) -> str:
    """new_id("pay") -> "pay_01J9Z3...". Prefix names the entity type."""
    if not _PREFIX_RE.match(prefix):
        raise ValueError(f"invalid id prefix {prefix!r}")
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    if not 0 <= ms < 2**48:
        raise ValueError("timestamp out of range")
    value = (ms << 80) | secrets.randbits(80)
    return f"{prefix}_{_encode(value, 26)}"


def is_valid_id(value: str, prefix: str | None = None) -> bool:
    if not _ID_RE.match(value):
        return False
    return prefix is None or value.startswith(prefix + "_")
