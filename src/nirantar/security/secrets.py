"""Secret resolution. Tables store references ("env:NAME", later "vault:path#key"), never secrets."""

from __future__ import annotations

import os

from nirantar.core.errors import NirantarError


class SecretNotFound(NirantarError):
    pass


def resolve_secret(ref: str | None) -> str:
    if not ref:
        raise SecretNotFound("no secret reference configured")
    scheme, _, name = ref.partition(":")
    if scheme == "env":
        value = os.environ.get(name, "")
        if not value:
            raise SecretNotFound(f"environment secret {name} is not set")
        return value
    if scheme == "literal" and os.environ.get("NIRANTAR_ENV", "local") in ("local", "test"):
        return name  # test fixtures only; rejected outside local/test
    raise SecretNotFound(f"unsupported secret scheme {scheme!r}")
