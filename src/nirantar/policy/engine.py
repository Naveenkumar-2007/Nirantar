"""Compliance Guardian policy engine (BB-§31). Deterministic; every rule cites a policy_id.

Every outbound action goes through `evaluate()` and gets exactly one of
ALLOW / DENY / REQUIRE_APPROVAL / REQUIRE_MORE_INFORMATION, with the policy hits
that produced it. The engine refuses to start if a cited policy_id is missing
from docs/compliance/policies.yaml (no regulation without a source, BB-§4).

Values that research marked partially/un-verified are tenant *config* that can
only be tightened (ADR-0004), never loosened below platform defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from nirantar.core.canonical import sha256_hex

ENGINE_VERSION = "policy-engine-1.0.0"
POLICIES_FILE = Path(__file__).resolve().parents[3] / "docs" / "compliance" / "policies.yaml"
INTERNAL_FILE = POLICIES_FILE.with_name("internal-policies.yaml")


class Outcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    REQUIRE_MORE_INFORMATION = "REQUIRE_MORE_INFORMATION"


_SEVERITY = {Outcome.ALLOW: 0, Outcome.REQUIRE_MORE_INFORMATION: 1, Outcome.REQUIRE_APPROVAL: 2, Outcome.DENY: 3}

CONTACT_ACTIONS = frozenset({"send_whatsapp", "send_sms", "send_email", "voice_call"})
# Mandatory = must reach the customer regardless of optional-contact rules. The last two answer a message the
# CUSTOMER just sent (WhatsApp service window): acknowledging it, or confirming their opt-out, is not outreach.
MANDATORY_KINDS = frozenset({"predebit_notice", "postdebit_notice", "agent_disclosure", "grievance_response",
                             "hardship_response", "reply_acknowledgement", "optout_confirmation"})
ALWAYS_APPROVE = frozenset({"legal_notice", "credit_draw", "payout_reschedule", "accept_dispute"})
CHANNEL_OF = {"send_whatsapp": "whatsapp", "send_sms": "sms", "send_email": "email", "voice_call": "voice"}


@dataclass(frozen=True)
class TenantPolicyConfig:
    """Platform defaults; a tenant may only make these stricter (see `tightened`)."""

    contact_window: tuple[time, time] = (time(9, 0), time(20, 0))    # platform default, all segments
    lending_window: tuple[time, time] = (time(8, 0), time(19, 0))    # IN-RBI-RBC-RECOVERY-HOURS-001
    mfi_window: tuple[time, time] = (time(9, 0), time(18, 0))        # IN-RBI-RBC-RECOVERY-MFI-HOURS-001
    max_contacts_7d: int = 3
    refund_approval_above_minor: int = 500_000                       # ₹5,000
    representment_approval_above_minor: int = 1_000_000              # ₹10,000
    discount_approval_above_minor: int = 50_000                      # ₹500 win-back discount needs a human
    voice_registered: bool = False                                    # TRAI 1600-series header on file
    lending_collections_enabled: bool = False                         # launch gate (ADR-0004 g)
    allow_debit_shift: bool = True                                    # lending tenants must opt in explicitly
    predebit_notice_hours: int = 24                                   # IN-RBI-EMANDATE-PREDEBIT-001 (minimum)

    def tightened(self, overrides: dict[str, Any]) -> TenantPolicyConfig:
        """Apply tenant overrides, rejecting any that loosen a platform default."""
        base = self
        vals: dict[str, Any] = {}
        for key, value in overrides.items():
            if key not in TenantPolicyConfig.__dataclass_fields__:
                raise ValueError(f"unknown policy setting {key}")
            cur = getattr(base, key)
            if key.endswith("_window"):
                s, e = (time.fromisoformat(v) if isinstance(v, str) else v for v in value)
                if s < cur[0] or e > cur[1]:
                    raise ValueError(f"{key} may only be narrowed")
                vals[key] = (s, e)
            elif key in ("max_contacts_7d", "refund_approval_above_minor", "representment_approval_above_minor",
                         "discount_approval_above_minor"):
                if int(value) > cur:
                    raise ValueError(f"{key} may only be lowered")
                vals[key] = int(value)
            elif key == "predebit_notice_hours":
                if int(value) < cur:
                    raise ValueError("pre-debit notice may only be lengthened")
                vals[key] = int(value)
            elif key == "allow_debit_shift":
                vals[key] = bool(value) and cur
            elif key in ("voice_registered", "lending_collections_enabled"):
                vals[key] = bool(value)  # facts about the tenant, recorded by onboarding
            else:
                raise ValueError(f"unknown policy setting {key}")
        return TenantPolicyConfig(**{**base.__dict__, **vals})


@dataclass(frozen=True)
class ActionRequest:
    tenant_id: str
    action_kind: str                     # e.g. send_whatsapp, voice_call, create_payment_link, create_refund
    segment: str                         # subscription | lending | mfi | sip | insurance | b2b
    now_utc: datetime
    customer_timezone: str = "Asia/Kolkata"
    purpose: str = "service"             # service | recovery | promotional | mandatory
    consents: dict[str, bool] = field(default_factory=dict)   # e.g. {"whatsapp": True, "promotional": False}
    opted_out_channels: frozenset[str] = frozenset()
    contacts_last_7d: int = 0
    message_text: str | None = None
    contains_offer: bool = False
    amount_minor: int | None = None
    agent_disclosure_sent: bool = True   # lending: agent details sent before first recovery contact
    proposed_debit_at: datetime | None = None
    mandatory_kind: str | None = None    # set for legally required communications
    environment: str = "local"


@dataclass(frozen=True)
class PolicyHit:
    policy_id: str
    outcome: Outcome
    message: str


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    hits: tuple[PolicyHit, ...]
    policy_version: str
    retry_after: datetime | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == Outcome.ALLOW


# Rule-based conduct screen (M12 baseline). Blocks threats, false urgency, third-party pressure.
# (plain-language label, pattern). Labels are what a merchant sees when wording is refused.
_CONDUCT_RULES = [(label, re.compile(p, re.IGNORECASE)) for label, p in (
    ("threat of police/arrest/jail", r"\bpolice\b"), ("threat of police/arrest/jail", r"\barrest"),
    ("threat of police/arrest/jail", r"\bjail\b"), ("criminal accusation", r"\bcriminal\b"),
    ("immediate legal-action threat", r"legal action (today|now|immediately)"),
    ("false urgency ('last chance')", r"\blast chance\b"),
    ("pressure deadline in minutes/hours", r"within \d+\s*(minutes?|hours?|hrs?)\b"),
    ("ultimatum ('or else')", r"\bor else\b"),
    ("threat to involve family/employer/others",
     r"\b(family|relatives?|friends?|employer|neighbou?rs?|colleagues?|office)\b.*\b(inform|tell|call|contact)"),
    ("threat to involve family/employer/others",
     r"\b(inform|tell|call|contact)\b.*\b(family|relatives?|friends?|employer|neighbou?rs?|colleagues?)\b"),
    ("shaming", r"\bshame\b"), ("blacklisting threat", r"\bblacklist"),
    ("credit-score threat", r"\bcibil\b.*\bruin"),
)]
_CONDUCT_PATTERNS = [rx for _, rx in _CONDUCT_RULES]


@lru_cache(maxsize=1)
def known_policy_ids() -> frozenset[str]:
    ids: set[str] = set()
    for path in (POLICIES_FILE, INTERNAL_FILE):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        records = data["policies"] if isinstance(data, dict) and "policies" in data else data
        ids |= {r["policy_id"] for r in records}
    return frozenset(ids)


@lru_cache(maxsize=1)
def policy_version() -> str:
    kb = POLICIES_FILE.read_text(encoding="utf-8") + INTERNAL_FILE.read_text(encoding="utf-8")
    return f"{ENGINE_VERSION}+kb-{sha256_hex(kb)[:12]}"


CITED = {
    "IN-RBI-EMANDATE-PREDEBIT-001", "IN-RBI-EMANDATE-OPTOUT-001", "IN-RBI-RBC-RECOVERY-HOURS-001",
    "IN-RBI-RBC-RECOVERY-MFI-HOURS-001", "IN-RBI-RBC-RECOVERY-CONDUCT-001", "IN-RBI-RBC-RECOVERY-AGENTINFO-001",
    "IN-RBI-RBC-RECOVERY-2026AMEND-001", "IN-TRAI-TCCCPR-VOICE-001", "IN-TRAI-TCCCPR-DLT-001",
    "IN-GOI-DPDP-NOTICE-CONSENT-001", "NIR-GOV-CONTACT-WINDOW-001", "NIR-GOV-FATIGUE-001",
    "NIR-GOV-MAKER-CHECKER-001", "NIR-GOV-CONSENT-001",
}


def check_policy_references() -> None:
    missing = CITED - known_policy_ids()
    if missing:
        raise RuntimeError(f"policy engine cites unknown policy ids: {sorted(missing)}")


def _in_window(local: datetime, window: tuple[time, time]) -> bool:
    return window[0] <= local.time() < window[1]


def _next_window_start(local: datetime, window: tuple[time, time]) -> datetime:
    start_today = local.replace(hour=window[0].hour, minute=window[0].minute, second=0, microsecond=0)
    return start_today if local.time() < window[0] else start_today + timedelta(days=1)


def conduct_violations(text: str) -> list[str]:
    return [p.pattern for p in _CONDUCT_PATTERNS if p.search(text)]


def conduct_violation_labels(text: str) -> list[str]:
    """Plain-language reasons (deduplicated) for UIs; `conduct_violations` keeps the exact patterns for audit."""
    return list(dict.fromkeys(label for label, rx in _CONDUCT_RULES if rx.search(text)))


def earliest_debit_time(now_utc: datetime, cfg: TenantPolicyConfig) -> datetime:
    """Earliest moment a (re)scheduled debit may execute: a fresh pre-debit notice sent now needs ≥ N hours."""
    return now_utc + timedelta(hours=cfg.predebit_notice_hours)


def evaluate(req: ActionRequest, cfg: TenantPolicyConfig | None = None) -> Decision:
    check_policy_references()
    cfg = cfg or TenantPolicyConfig()
    hits: list[PolicyHit] = []
    retry_after: datetime | None = None
    local = req.now_utc.astimezone(ZoneInfo(req.customer_timezone))
    is_contact = req.action_kind in CONTACT_ACTIONS
    mandatory = req.mandatory_kind in MANDATORY_KINDS

    def hit(pid: str, outcome: Outcome, msg: str) -> None:
        hits.append(PolicyHit(pid, outcome, msg))

    if is_contact:
        channel = CHANNEL_OF[req.action_kind]
        # consent / opt-out (DPDP notice & consent; e-mandate opt-out for debit communications)
        if channel in req.opted_out_channels and not mandatory:
            hit("NIR-GOV-CONSENT-001", Outcome.DENY, f"customer opted out of {channel}")
        if not req.consents.get(channel, False) and not mandatory:
            hit("NIR-GOV-CONSENT-001", Outcome.DENY, f"no recorded consent for {channel}")
        # TRAI: any offer makes it promotional; promotional needs its own consent
        if req.contains_offer or req.purpose == "promotional":
            if not req.consents.get("promotional", False):
                hit("IN-TRAI-TCCCPR-DLT-001", Outcome.DENY, "offer content requires promotional consent")
        if channel == "voice" and not cfg.voice_registered:
            hit("IN-TRAI-TCCCPR-VOICE-001", Outcome.DENY, "tenant has no registered service voice header")
        # contact windows (customer's local time)
        window = cfg.contact_window
        pid = "NIR-GOV-CONTACT-WINDOW-001"  # platform default window (ADR-0004 b)
        if req.segment == "lending" and req.purpose == "recovery":
            window, pid = cfg.lending_window, "IN-RBI-RBC-RECOVERY-HOURS-001"
        elif req.segment == "mfi" and req.purpose == "recovery":
            window, pid = cfg.mfi_window, "IN-RBI-RBC-RECOVERY-MFI-HOURS-001"
        if not _in_window(local, window) and not mandatory:
            retry_after = _next_window_start(local, window).astimezone(req.now_utc.tzinfo)
            hit(pid, Outcome.DENY, f"outside contact window {window[0]:%H:%M}-{window[1]:%H:%M} (local {local:%H:%M})")
        # fatigue budget
        if req.contacts_last_7d >= cfg.max_contacts_7d and not mandatory:
            hit("NIR-GOV-FATIGUE-001", Outcome.DENY,
                f"contact budget exhausted ({req.contacts_last_7d}/{cfg.max_contacts_7d} in 7 days)")
        # lending specifics
        if req.segment in ("lending", "mfi") and req.purpose == "recovery":
            if not cfg.lending_collections_enabled and req.environment == "production":
                hit("IN-RBI-RBC-RECOVERY-2026AMEND-001", Outcome.DENY,
                    "lending collections launch gate closed pending counsel confirmation")
            if not req.agent_disclosure_sent:
                hit("IN-RBI-RBC-RECOVERY-AGENTINFO-001", Outcome.REQUIRE_MORE_INFORMATION,
                    "recovery agent details must be sent to the borrower before first contact")
    # conduct screen on any customer-facing text
    if req.message_text:
        for pattern in conduct_violations(req.message_text):
            hit("IN-RBI-RBC-RECOVERY-CONDUCT-001", Outcome.DENY, f"conduct violation: /{pattern}/")
    # money actions
    if req.action_kind == "create_refund" and (req.amount_minor or 0) > cfg.refund_approval_above_minor:
        hit("NIR-GOV-MAKER-CHECKER-001", Outcome.REQUIRE_APPROVAL, "refund above tenant threshold")
    if req.action_kind == "submit_representment" and (req.amount_minor or 0) > cfg.representment_approval_above_minor:
        hit("NIR-GOV-MAKER-CHECKER-001", Outcome.REQUIRE_APPROVAL, "representment above tenant threshold")
    if req.action_kind == "grant_discount" and (req.amount_minor or 0) > cfg.discount_approval_above_minor:
        hit("NIR-GOV-MAKER-CHECKER-001", Outcome.REQUIRE_APPROVAL, "win-back discount above tenant threshold")
    if req.action_kind in ALWAYS_APPROVE:
        hit("NIR-GOV-MAKER-CHECKER-001", Outcome.REQUIRE_APPROVAL, f"{req.action_kind} always needs a human")
    if req.action_kind == "shift_debit":
        if req.segment in ("lending", "mfi") and not cfg.allow_debit_shift:
            hit("IN-RBI-EMANDATE-PREDEBIT-001", Outcome.DENY, "lender has not allowed debit shifts")
        if req.proposed_debit_at is None:
            hit("IN-RBI-EMANDATE-PREDEBIT-001", Outcome.REQUIRE_MORE_INFORMATION, "proposed debit time missing")
        elif req.proposed_debit_at < earliest_debit_time(req.now_utc, cfg):
            hit("IN-RBI-EMANDATE-PREDEBIT-001", Outcome.DENY,
                f"new debit needs a fresh pre-debit notice ≥{cfg.predebit_notice_hours}h ahead")
    outcome = max((h.outcome for h in hits), key=lambda o: _SEVERITY[o], default=Outcome.ALLOW)
    return Decision(outcome, tuple(hits), policy_version(), retry_after if outcome == Outcome.DENY else None)
