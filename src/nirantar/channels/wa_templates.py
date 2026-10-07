"""Meta message templates derived from Nirantar's template registry.

Outside the customer's 24-hour window WhatsApp only delivers templates Meta has approved. Each registry key that
starts a conversation has a Meta template whose body is the registry's platform-default text with {placeholders}
turned into Meta's positional {{1}}..{{n}}. Meta rejects a body that ends with a variable, so those get a short
closing line. Facts (amount, date, link) are still filled in by code at send time, never by the template.

    uv run python -m nirantar.channels.wa_templates status    # read: what Meta has, what is approved
    uv run python -m nirantar.channels.wa_templates submit    # submit missing templates for Meta review
    uv run python -m nirantar.channels.wa_templates check     # token validity/expiry, number health, approvals
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from nirantar.channels.whatsapp import WhatsAppCloud, WhatsAppError
from nirantar.settings import templates as registry

LANGS = {"en": "en", "hi": "hi", "te": "te"}           # registry language → Meta language code
CLOSING = {"en": "Thank you.", "hi": "धन्यवाद।", "te": "ధన్యవాదాలు."}
EXAMPLES = {"name": "Priya", "amount": "₹999.00", "plan": "Chai Club monthly", "date": "05 Oct 2026",
            "link": "https://rzp.io/i/example", "offer": "Your restart comes with 10% off the first payment.",
            "ref": "pay_Q4xY7ExAmPlE", "business": "Chai Club", "number": "INV-2026-041",
            "deadline": "12 Oct 2026", "count": "3", "items": "2 x Masala chai blend",
            "list": "INV-041 ₹4,000.00 (due 05 Oct); INV-044 ₹2,500.00 (due 10 Oct)"}


@dataclass(frozen=True)
class MetaTemplate:
    key: str
    meta_name: str
    category: str               # UTILITY | MARKETING
    params: tuple[str, ...]     # registry placeholders in Meta's positional order


CATALOG = (
    MetaTemplate("whatsapp.recovery", "nirantar_payment_retry", "UTILITY", ("name", "amount", "plan", "link")),
    MetaTemplate("whatsapp.payment_due", "nirantar_payment_due", "UTILITY", ("name", "plan", "amount", "date", "link")),
    # cart reminders are MARKETING under Meta's policy: sent only with promotional consent (ADR-0028)
    MetaTemplate("whatsapp.checkout_reminder", "nirantar_checkout_reminder", "MARKETING",
                 ("name", "business", "items", "amount", "link")),
    MetaTemplate("whatsapp.checkout_payment_retry", "nirantar_checkout_payment_retry", "MARKETING",
                 ("name", "amount", "business", "link")),
    MetaTemplate("whatsapp.checkout_bank_issue", "nirantar_checkout_bank_issue", "MARKETING",
                 ("name", "amount", "business", "link")),
    MetaTemplate("whatsapp.checkout_follow_up", "nirantar_checkout_follow_up", "MARKETING",
                 ("name", "business", "amount", "link")),
    MetaTemplate("whatsapp.invoice_statement", "nirantar_invoice_statement", "UTILITY",
                 ("name", "business", "count", "amount", "list", "link")),
    MetaTemplate("whatsapp.invoice_reminder", "nirantar_invoice_reminder", "UTILITY",
                 ("name", "business", "number", "amount", "date", "link")),
    MetaTemplate("whatsapp.invoice_overdue", "nirantar_invoice_overdue", "UTILITY",
                 ("name", "business", "number", "amount", "date", "link")),
    MetaTemplate("whatsapp.invoice_final", "nirantar_invoice_final", "UTILITY",
                 ("name", "business", "number", "amount", "deadline", "link")),
    MetaTemplate("whatsapp.bank_issue_retry", "nirantar_bank_issue_retry", "UTILITY",
                 ("name", "plan", "amount", "link")),
    MetaTemplate("whatsapp.promise_reminder", "nirantar_promise_reminder", "UTILITY",
                 ("name", "plan", "amount", "link")),
    MetaTemplate("whatsapp.payment_receipt", "nirantar_payment_receipt", "UTILITY",
                 ("name", "amount", "plan", "date", "ref")),
    MetaTemplate("whatsapp.predebit_notice", "nirantar_predebit_notice", "UTILITY", ("amount", "date", "plan")),
    MetaTemplate("whatsapp.mandate_reauth", "nirantar_autopay_renew", "UTILITY", ("name", "plan", "date", "link")),
    MetaTemplate("whatsapp.mandate_resume", "nirantar_autopay_resume", "UTILITY", ("name", "plan", "date")),
    MetaTemplate("whatsapp.winback", "nirantar_winback", "MARKETING", ("name", "plan", "offer", "link")),
)
_FIELD = re.compile(r"\{([a-z_]+)\}")


def meta_body(t: MetaTemplate, language: str) -> tuple[str, list[str]]:
    """(Meta body with {{n}}, example values in order). Uses ONLY placeholders the body contains, in body order."""
    body = registry.platform_default(t.key, language)
    if body is None:
        raise KeyError(f"no {language} text for {t.key}")
    order = [f for f in dict.fromkeys(_FIELD.findall(body))]
    unknown = set(order) - set(t.params)
    if unknown:
        raise ValueError(f"{t.key}: placeholders {sorted(unknown)} not in the catalog params")
    out = _FIELD.sub(lambda m: "{{" + str(order.index(m.group(1)) + 1) + "}}", body).strip()
    if out.endswith("}}"):
        out = f"{out}\n\n{CLOSING[language]}"
    return out, [EXAMPLES[f] for f in order]


def body_params(t: MetaTemplate, language: str) -> list[str]:
    body = registry.platform_default(t.key, language) or ""
    return list(dict.fromkeys(_FIELD.findall(body)))


def status(engine: Engine, wa: WhatsAppCloud) -> list[dict[str, Any]]:
    """Refresh config.whatsapp_templates from Meta (read-only on Meta's side)."""
    remote = {(t["name"], t["language"]): t for t in wa.list_templates()}
    rows = []
    with engine.begin() as c:
        for t in CATALOG:
            for lang, meta_lang in LANGS.items():
                r = remote.get((t.meta_name, meta_lang))
                rows.append({"key": t.key, "language": lang, "meta_name": t.meta_name,
                             "status": r["status"] if r else "NOT_SUBMITTED"})
                if r:
                    c.execute(text("INSERT INTO config.whatsapp_templates (template_key, language, meta_name, "
                                   "meta_language, category, params, status, meta_id, reason, updated_at) VALUES "
                                   "(:k, :l, :n, :ml, :c, :p, :s, :id, :r, now()) ON CONFLICT (template_key, language) "
                                   "DO UPDATE SET status=EXCLUDED.status, meta_id=EXCLUDED.meta_id, "
                                   "reason=EXCLUDED.reason, params=EXCLUDED.params, updated_at=now()"),
                              {"k": t.key, "l": lang, "n": t.meta_name, "ml": meta_lang, "c": r.get("category",
                                                                                                t.category),
                               "p": body_params(t, lang), "s": r["status"], "id": r.get("id"),
                               "r": r.get("rejected_reason")})
    return rows


def submit(engine: Engine, wa: WhatsAppCloud) -> list[dict[str, Any]]:
    """Submit every catalog template Meta does not have yet. Meta reviews them (minutes to hours)."""
    have = {(t["name"], t["language"]) for t in wa.list_templates()}
    results = []
    for t in CATALOG:
        for lang, meta_lang in LANGS.items():
            if (t.meta_name, meta_lang) in have:
                continue
            body, example = meta_body(t, lang)
            try:
                out = wa.create_template(t.meta_name, meta_lang, t.category, body, example)
                results.append({"key": t.key, "language": lang, "status": out.get("status", "PENDING")})
            except WhatsAppError as exc:
                results.append({"key": t.key, "language": lang, "error": str(exc)[:200]})
    status(engine, wa)
    return results


def check(wa: WhatsAppCloud) -> dict[str, Any]:
    """Is the channel usable right now? Token validity/expiry, number health, template approvals (read-only)."""
    from datetime import UTC, datetime

    out: dict[str, Any] = {}
    try:
        dbg = wa._call("GET", wa._url("debug_token"), params={"input_token": wa.access_token})["data"]
        exp = int(dbg.get("expires_at") or 0)
        out["token"] = {"valid": bool(dbg.get("is_valid")), "type": dbg.get("type"),
                        "expires": "never" if exp == 0 else datetime.fromtimestamp(exp, UTC).isoformat(),
                        "scopes": dbg.get("scopes")}
        if exp:
            out["warning"] = ("this token expires; create a permanent System User token (Business Settings → "
                              "System users → Generate token, expiration: Never)")
    except WhatsAppError as exc:
        out["token"] = {"valid": False, "error": exc.title}
        if exc.code == 190:
            out["fix"] = ("the access token is expired or revoked: create a permanent System User token and set "
                          "WHATSAPP_ACCESS_TOKEN in .env")
        return out
    out["number"] = wa._call("GET", wa._url(wa.phone_number_id), params={
        "fields": "display_phone_number,verified_name,quality_rating,code_verification_status,messaging_limit_tier"})
    out["templates"] = {f"{t['name']}@{t['language']}": t["status"] for t in wa.list_templates()
                        if t["name"].startswith("nirantar_")}
    return out


def main(argv: list[str]) -> None:
    import json

    from nirantar.core.dotenv import load_dotenv as _load_dotenv
    from nirantar.db.session import get_engine

    _load_dotenv()
    wa = WhatsAppCloud.from_env()
    if wa is None:
        raise SystemExit("WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN not set")
    action = argv[0] if argv else "status"
    if action == "check":
        print(json.dumps(check(wa), indent=1, ensure_ascii=False))
        return
    out = submit(get_engine(), wa) if action == "submit" else status(get_engine(), wa)
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
