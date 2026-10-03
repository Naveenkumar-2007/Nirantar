"""Operator command: give a person (who has signed in at least once) a role in an existing business.

    uv run python -m nirantar.security.members add you@example.com ten_01ABC... owner
    uv run python -m nirantar.security.members list you@example.com

Inside the product, owners invite members themselves (P8 team management); this is for operators and for
attaching an existing demo/seeded business to a newly registered account.
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from nirantar.db.session import get_engine
from nirantar.security.oidc import add_membership


def main(argv: list[str]) -> None:
    from nirantar.api.serve import _load_dotenv

    _load_dotenv()
    engine = get_engine()
    if len(argv) >= 2 and argv[0] in ("add", "list"):
        with engine.connect() as c:
            sub = c.execute(text("SELECT user_sub FROM core.users WHERE lower(email)=lower(:e)"),
                            {"e": argv[1]}).scalar_one_or_none()
        if sub is None:
            raise SystemExit(f"{argv[1]} has not signed in yet; sign in once, then run this again")
        if argv[0] == "add":
            if len(argv) != 4:
                raise SystemExit("usage: add <email> <tenant_id> <role>")
            add_membership(engine, sub, argv[2], [argv[3]], invited_by="operator-cli")
            print(f"{argv[1]} is now {argv[3]} of {argv[2]}")
        else:
            with engine.connect() as c:
                for r in c.execute(text("SELECT tenant_id, roles, status FROM core.user_memberships WHERE "
                                        "user_sub=:s"), {"s": sub}):
                    print(r.tenant_id, list(r.roles), r.status)
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
