"""Exotel REST client for outbound calls (P11, ADR-0026).

An outbound recovery call = Exotel's "connect a number to a call flow" API: Exotel dials the customer from the
business's ExoPhone and runs the flow (App) whose Voicebot applet streams audio to Nirantar's voice gateway
(`/voice/exotel`). The only thing the call carries is a signed call token (CustomField); the gateway resolves the
business, amount and language server-side from it — nothing the network sends is trusted for content.

Config (deployment): EXOTEL_ACCOUNT_SID, EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_SUBDOMAIN (e.g. api.exotel.com),
EXOTEL_CALLER_ID (the ExoPhone), EXOTEL_FLOW_ID (the App with the Voicebot applet).
Trial accounts can only call numbers verified in the Exotel dashboard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from nirantar.core.errors import NirantarError


class ExotelError(NirantarError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"exotel {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class PlacedCall:
    call_sid: str
    status: str


# Exotel call status → Nirantar (comms.calls.status)
STATUS = {"queued": "requested", "ringing": "ringing", "in-progress": "in_progress", "completed": "completed",
          "busy": "busy", "no-answer": "no_answer", "failed": "failed", "canceled": "canceled"}


class ExotelClient:
    def __init__(self, account_sid: str, api_key: str, api_token: str, subdomain: str, caller_id: str, flow_id: str,
                 *, client: httpx.Client | None = None) -> None:
        if not all((account_sid, api_key, api_token, subdomain)):
            raise ValueError("exotel credentials required")
        host = subdomain.removeprefix("https://").removeprefix("http://").strip("/")
        self.sid, self.caller_id, self.flow_id = account_sid, caller_id, flow_id
        self._base = f"https://{host}/v1/Accounts/{account_sid}"
        self._http = client or httpx.Client(auth=(api_key, api_token), timeout=15.0)

    @classmethod
    def from_env(cls) -> ExotelClient | None:
        keys = ("EXOTEL_ACCOUNT_SID", "EXOTEL_API_KEY", "EXOTEL_API_TOKEN", "EXOTEL_SUBDOMAIN", "EXOTEL_CALLER_ID",
                "EXOTEL_FLOW_ID")
        vals = [os.environ.get(k, "").strip() for k in keys]
        return cls(*vals) if all(vals) else None

    def flow_url(self) -> str:
        return f"http://my.exotel.com/{self.sid}/exoml/start_voice/{self.flow_id}"

    def place_call(self, to_number: str, call_token: str, status_callback: str | None,
                   time_limit_s: int = 300) -> PlacedCall:
        data: dict[str, Any] = {"From": to_number, "CallerId": self.caller_id, "Url": self.flow_url(),
                                "CustomField": call_token, "TimeLimit": time_limit_s, "TimeOut": 40}
        if status_callback:
            data["StatusCallback"] = status_callback
            data["StatusCallbackEvents[0]"] = "terminal"
            data["StatusCallbackContentType"] = "application/json"
        r = self._http.post(f"{self._base}/Calls/connect.json", data=data)
        if r.status_code >= 300:
            raise ExotelError(r.status_code, _error(r))
        call = r.json().get("Call", {})
        return PlacedCall(str(call["Sid"]), STATUS.get(str(call.get("Status", "queued")).lower(), "requested"))

    def get_call(self, call_sid: str) -> dict[str, Any]:
        r = self._http.get(f"{self._base}/Calls/{call_sid}.json")
        if r.status_code >= 300:
            raise ExotelError(r.status_code, _error(r))
        out: dict[str, Any] = r.json().get("Call", {})
        return out


def _error(r: httpx.Response) -> str:
    try:
        body = r.json()
        rest = body.get("RestException", body)
        return str(rest.get("Message") or rest)[:200]
    except ValueError:
        return r.text[:200]


_DEFAULT: list[ExotelClient | None] = []


def default_client() -> ExotelClient | None:
    """The deployment's Exotel account (env), built once; None when voice is not configured."""
    from nirantar.core.demo import is_demo

    if is_demo():                                   # the demo never places a call
        return None
    if not _DEFAULT:
        _DEFAULT.append(ExotelClient.from_env())
    return _DEFAULT[0]
