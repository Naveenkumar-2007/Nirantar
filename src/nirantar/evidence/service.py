"""Evidence extraction and verification.

Payment screenshots (BB-§28): OCR → field extraction → tamper signals → cross-check against the provider.
A screenshot is NEVER treated as proof: the verdict is `matched_provider_record` only if the provider has a
captured payment that matches; otherwise `not_verified`. Tamper signals are a transparent baseline (editing
software metadata, malformed UTR, amount/record mismatch) — a trained image-forgery model is future work.

Voice notes: Sarvam STT → redacted transcript → intent. AA statements: deterministic parser → salary-credit
days → cash window (feeds M2).
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.core.canonical import sha256_hex
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.evidence import objectstore
from nirantar.ml.cash_window import estimate
from nirantar.payments.domain import PaymentProvider, PaymentStatus, ProviderError

EDITING_SOFTWARE = ("photoshop", "gimp", "canva", "picsart", "snapseed", "lightroom", "pixlr", "paint.net",
                    "fotor", "remini")
_AMOUNT = re.compile(r"(?:₹|(?<![A-Za-z0-9])(?:rs\.?|inr))\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)
# Extract any plausible reference number (6–20 digits); validity (12-digit UPI RRN) is checked separately so a
# malformed UTR is flagged instead of silently ignored.
_UTR = re.compile(r"(?:utr|rrn|ref(?:erence)?(?:no)?|upiref)\s*[:.#]?\s*([0-9]{6,20})", re.IGNORECASE)
_SUCCESS = re.compile(r"success|paid|completed|సఫల|सफल", re.IGNORECASE)
_FAIL = re.compile(r"fail|declin|pending|విఫల|असफल", re.IGNORECASE)


@dataclass
class EvidenceObject:
    case_id: str | None
    customer_id: str | None
    modality: str
    source_uri: str
    extracted_fields: dict[str, Any]
    language: str | None
    confidence: float
    verified_against: str | None
    hash: str
    created_at: datetime
    evidence_id: str = field(default_factory=lambda: new_id("evd"))


def save(conn: Connection, tenant_id: str, ev: EvidenceObject) -> str:
    conn.execute(text("INSERT INTO ai.evidence (tenant_id, evidence_id, case_id, customer_id, modality, source_uri, "
                      "extracted_fields, language, confidence, verified_against, hash, created_at) VALUES (:t, :e, :c, "
                      ":cu, :m, :s, CAST(:f AS jsonb), :l, :conf, :v, :h, :n)"),
                 {"t": tenant_id, "e": ev.evidence_id, "c": ev.case_id, "cu": ev.customer_id, "m": ev.modality,
                  "s": ev.source_uri, "f": json.dumps(ev.extracted_fields, default=str), "l": ev.language,
                  "conf": ev.confidence, "v": ev.verified_against, "h": ev.hash, "n": ev.created_at})
    return ev.evidence_id


# ---------------------------------------------------------------- screenshots
def _ocr_lines(image: bytes) -> tuple[list[str], float]:
    result, _ = _engine()(image)
    if not result:
        return [], 0.0
    return [r[1] for r in result], float(sum(r[2] for r in result) / len(result))


_OCR: Any = None


def _engine() -> Any:
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def image_metadata(image: bytes) -> dict[str, str]:
    from PIL import Image

    with Image.open(io.BytesIO(image)) as im:
        meta = {str(k): str(v) for k, v in (im.info or {}).items() if isinstance(v, str | bytes)}
        exif = im.getexif()
        if exif and 305 in exif:           # EXIF tag 305 = Software
            meta["exif_software"] = str(exif[305])
    return meta


def extract_payment_fields(lines: list[str]) -> dict[str, Any]:
    joined = " ".join(lines)
    amount = None
    for m in _AMOUNT.findall(joined):  # lines are space-joined, so the word-boundary rule holds
        try:
            amount = Money.of(m.replace(",", "")).minor
            break
        except Exception:  # noqa: S112 - an unreadable figure is simply not an amount
            continue
    utr = _UTR.search(joined.replace(" ", ""))
    status = "success" if _SUCCESS.search(joined) and not _FAIL.search(joined) else \
        "failed_or_pending" if _FAIL.search(joined) else "unknown"
    return {"amount_minor": amount, "utr": utr.group(1) if utr else None, "status_text": status}


def tamper_signals(fields: dict[str, Any], meta: dict[str, str], ocr_conf: float) -> list[str]:
    signals = []
    software = " ".join(v.lower() for k, v in meta.items() if "software" in k.lower())
    if any(tool in software for tool in EDITING_SOFTWARE):
        signals.append(f"edited_with:{software[:40]}")
    if fields.get("utr") and len(fields["utr"]) != 12:
        signals.append("utr_not_12_digits")        # UPI RRNs are 12 digits
    if ocr_conf and ocr_conf < 0.6:
        signals.append("low_ocr_confidence")
    if fields.get("amount_minor") is None:
        signals.append("no_amount_found")
    return signals


def screenshot_evidence(conn: Connection, tenant_id: str, image: bytes, *, customer_id: str | None,
                        case_id: str | None, provider: PaymentProvider, claimed_at: datetime,
                        now: datetime) -> EvidenceObject:
    uri, sha = objectstore.put(tenant_id, "screenshot", image, "image/png")
    lines, conf = _ocr_lines(image)
    fields = extract_payment_fields(lines)
    signals = tamper_signals(fields, image_metadata(image), conf)
    verdict, match = "not_verified", None
    if fields["amount_minor"] is not None:
        try:
            window = timedelta(hours=48)
            candidates = list(provider.list_payments(claimed_at - window, claimed_at + window))
        except ProviderError as exc:
            candidates, signals = [], [*signals, f"provider_unreachable:{str(exc)[:40]}"]
        for p in candidates:
            if p.status == PaymentStatus.CAPTURED and p.amount.minor == fields["amount_minor"] and \
                    (customer_id is None or p.customer_ref in (None, customer_id)):
                verdict, match = "matched_provider_record", p.provider_payment_id
                break
    if verdict != "matched_provider_record" and fields["status_text"] == "success":
        signals.append("claims_success_without_provider_record")
    ev = EvidenceObject(case_id, customer_id, "screenshot", uri,
                        {**fields, "ocr_lines": lines, "tamper_signals": signals, "verdict": verdict,
                         "matched_provider_payment_id": match,
                         "rule": "screenshot is never authoritative; provider/ledger state wins"},
                        None, round(conf, 3), "provider" if match else None, sha, now)
    save(conn, tenant_id, ev)
    return ev


# ---------------------------------------------------------------- voice notes
def voice_note_evidence(conn: Connection, tenant_id: str, audio_pcm: bytes, rate: int, language: str, speech: Any,
                        *, customer_id: str | None, case_id: str | None, now: datetime) -> EvidenceObject:
    from nirantar.voice.audio import pcm_to_wav
    from nirantar.voice.turn import intent_of, redact_secrets

    wav = pcm_to_wav(audio_pcm, rate)
    uri, sha = objectstore.put(tenant_id, "voice_note", wav, "audio/wav")
    tr = speech.stt(audio_pcm, rate, language)
    clean, secret = redact_secrets(tr.text)
    ev = EvidenceObject(case_id, customer_id, "voice_note", uri,
                        {"transcript_redacted": clean, "intent": intent_of(clean), "secret_detected": secret,
                         "stt_ms": tr.latency_ms}, tr.language_code or language, 0.8, "stt", sha, now)
    save(conn, tenant_id, ev)
    return ev


# ---------------------------------------------------------------- Account Aggregator statements
_SALARY = re.compile(r"\b(sal(ary)?|payroll|wages|stipend)\b", re.IGNORECASE)


def salary_days(aa_json: dict[str, Any]) -> list[date]:
    """Parse AA deposit FI data (Account → Transactions → Transaction[]) and return salary credit dates."""
    txns = aa_json.get("Account", {}).get("Transactions", {}).get("Transaction", [])
    out = []
    for t in txns:
        if str(t.get("type", "")).upper() == "CREDIT" and _SALARY.search(str(t.get("narration", ""))):
            out.append(date.fromisoformat(str(t.get("valueDate") or t.get("transactionTimestamp"))[:10]))
    return sorted(out)


def aa_statement_evidence(conn: Connection, tenant_id: str, aa_json: dict[str, Any], *, customer_id: str,
                          consent_id: str, now: datetime) -> EvidenceObject:
    raw = json.dumps(aa_json, sort_keys=True).encode()
    uri, sha = objectstore.put(tenant_id, "aa_json", raw, "application/json")
    days = salary_days(aa_json)
    cw = estimate([d.day for d in days], [1.0] * len(days))
    ev = EvidenceObject(None, customer_id, "aa_json", uri,
                        {"consent_id": consent_id, "salary_credit_dates": [d.isoformat() for d in days],
                         "cash_window_day": round(cw.day, 1) if cw.day else None,
                         "cash_window_confidence": round(cw.confidence, 3)},
                        None, cw.confidence, "account_aggregator", sha, now)
    save(conn, tenant_id, ev)
    return ev


def evidence_hash_ok(ev_hash: str, data: bytes) -> bool:
    import hashlib

    return hashlib.sha256(data).hexdigest() == ev_hash


def facts_hash(facts: dict[str, Any]) -> str:
    return sha256_hex(facts)
