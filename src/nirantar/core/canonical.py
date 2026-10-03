"""Canonical JSON + SHA-256. Used for audit chains, input/output hashes and event payload hashes.

Same logical value -> same bytes -> same hash, regardless of dict ordering.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from nirantar.core.money import Money


def _default(value: Any) -> Any:
    if isinstance(value, Money):
        return {"minor": value.minor, "currency": value.currency}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be canonicalised")
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, default=_default, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
