"""Evidence pack PDF for a dispute representment (P5, ADR-0015).

Facts only from verified sources (system of record, contact log, provider re-fetch). The narrative summary is the
Dispute Defender's draft, which is screened for conduct and never introduces amounts or dates of its own: every
figure in the pack is printed from the evidence items. Uploaded to the provider's Documents API on submission.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from nirantar.disputes.evidence import EvidenceItem

TITLES = {"mandate_record": "Mandate on record", "predebit_notice": "Pre-debit notice sent",
          "payment_verification": "Payment verified with the provider",
          "no_prior_opt_out": "No opt-out before the debit"}


def _fmt(v: Any) -> str:
    return "—" if v is None else escape(str(v))


def render_pack(*, merchant: str, dispute: dict[str, Any], items: Sequence[EvidenceItem], summary: str,
                generated_at: datetime) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"Dispute evidence {dispute['provider_dispute_id']}")
    st = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph(f"Dispute evidence — {escape(merchant)}", st["Title"]),
        Paragraph(f"Dispute {_fmt(dispute['provider_dispute_id'])} · payment {_fmt(dispute['provider_payment_id'])} · "
                  f"amount {_fmt(dispute['amount_minor'] / 100)} {_fmt(dispute['currency'])} · reason "
                  f"{_fmt(dispute['reason_code'])}", st["Normal"]),
        Paragraph(f"Generated {generated_at:%Y-%m-%d %H:%M} UTC from verified records.", st["Italic"]),
        Spacer(1, 6 * mm), Paragraph("Summary", st["Heading2"]), Paragraph(escape(summary), st["BodyText"]),
        Spacer(1, 6 * mm), Paragraph("Evidence", st["Heading2"]),
    ]
    rows = [["Evidence", "Supports merchant", "Source", "Facts"]]
    for it in items:
        facts = "<br/>".join(f"{escape(k)}: {_fmt(v)}" for k, v in it.facts.items())
        rows.append([TITLES.get(it.kind, it.kind), "yes" if it.supports_merchant else "no", it.verified_against,
                     Paragraph(facts, st["BodyText"])])
    table = Table(rows, colWidths=[42 * mm, 24 * mm, 30 * mm, 78 * mm], repeatRows=1)
    table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("GRID", (0, 0), (-1, -1), 0.4, "#999999"), ("FONTSIZE", (0, 0), (-1, -1), 8.5)]))
    story.append(table)
    doc.build(story)
    return buf.getvalue()
