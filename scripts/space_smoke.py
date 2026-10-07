"""Smoke test a deployed Nirantar demo Space: wait for RUNNING, then check /ready, /version and the web app.

    python scripts/space_smoke.py --space Naveen-2007/nirantar --url https://naveen-2007-nirantar.hf.space

HF_TOKEN (env) is sent as a bearer token so a private Space can be checked. Exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx
from huggingface_hub import HfApi


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--timeout", type=int, default=2400)
    a = ap.parse_args()
    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)
    deadline = time.monotonic() + a.timeout
    stage = ""
    while time.monotonic() < deadline:
        stage = api.get_space_runtime(a.space).stage
        print(f"space stage: {stage}", flush=True)
        if stage == "RUNNING":
            break
        if stage in ("BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"):
            print(f"space failed: {stage}", file=sys.stderr)
            return 1
        time.sleep(20)
    else:
        print(f"timed out waiting for RUNNING (last stage {stage})", file=sys.stderr)
        return 1

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=a.url.rstrip("/"), headers=headers, timeout=20, follow_redirects=True) as c:
        for _ in range(60):                       # the container seeds its demo business after it starts
            r = c.get("/ready")
            if r.status_code == 200:
                break
            time.sleep(10)
        checks = {
            "ready": r.status_code == 200 and r.json().get("ready") is True,
            "version is demo": c.get("/version").json().get("demo") is True,
            "web app": c.get("/").status_code == 200,
            "unsigned webhook is refused": c.post("/webhooks/razorpay/ten_doesnotexist",
                                                  content=b"{}").status_code in (400, 401, 403, 404),
        }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
