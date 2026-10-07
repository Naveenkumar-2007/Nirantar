"""Demo mode (NIRANTAR_DEMO=1): the public, self-resetting showcase deployment.

In demo mode nothing can leave the building: messages go to the mock sink, voice is off, and only the mock payment
provider can be connected. `assert_safe` makes a demo process refuse to start if real channel or payment credentials
are present, so a misconfigured demo cannot message, call or charge anyone.
"""

from __future__ import annotations

import os

REAL_CREDENTIALS = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_APP_SECRET", "EXOTEL_API_TOKEN", "EXOTEL_API_KEY",
                    "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET", "CASHFREE_CLIENT_SECRET")


class DemoModeViolation(RuntimeError):
    pass


def is_demo() -> bool:
    return os.environ.get("NIRANTAR_DEMO") == "1"


def assert_safe() -> None:
    if not is_demo():
        return
    present = [k for k in REAL_CREDENTIALS if os.environ.get(k)]
    if present:
        raise DemoModeViolation(f"demo mode refuses real credentials: {', '.join(present)}")
    if os.environ.get("NIRANTAR_ENV") == "production":
        raise DemoModeViolation("demo mode cannot run with NIRANTAR_ENV=production")
