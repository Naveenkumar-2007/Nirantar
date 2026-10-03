"""Load the repo-root .env for local processes (never overrides variables already set by the environment)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


def load_dotenv(path: Path = ROOT_ENV) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
