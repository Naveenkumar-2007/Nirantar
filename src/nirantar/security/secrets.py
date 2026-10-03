"""Secret resolution. Tables store references, never secrets:
  env:NAME          deployment-level secret from the environment
  tenant:<id>       a business's own credential, encrypted with its tenant key (core.tenant_secrets, ADR-0019)
  literal:value     local/test fixtures only"""

from __future__ import annotations

import os
from typing import Any

from nirantar.core.errors import NirantarError


class SecretNotFound(NirantarError):
    pass


def resolve_secret(ref: str | None, *, tenant_id: str | None = None, engine: Any = None) -> str:
    if not ref:
        raise SecretNotFound("no secret reference configured")
    scheme, _, name = ref.partition(":")
    if scheme == "tenant":
        if tenant_id is None or engine is None:
            raise SecretNotFound("tenant secrets need the tenant and a database engine")
        from nirantar.security.vault import read_secret

        value = read_secret(engine, tenant_id, name)
        if value is None:
            raise SecretNotFound("tenant secret not found or retired")
        return value
    if scheme == "env":
        value = os.environ.get(name, "")
        if not value:
            raise SecretNotFound(f"environment secret {name} is not set")
        return value
    if scheme == "literal" and os.environ.get("NIRANTAR_ENV", "local") in ("local", "test"):
        return name  # test fixtures only; rejected outside local/test
    raise SecretNotFound(f"unsupported secret scheme {scheme!r}")
