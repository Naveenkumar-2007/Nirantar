"""Grounded answers with verified citations; fail closed for compliance questions.

The LLM must answer ONLY from the retrieved chunks and cite each claim with (chunk_id, verbatim quote).
The citation checker accepts a citation only if the chunk was actually retrieved AND the quote appears in
that chunk (whitespace/case-normalised). No valid citation → "insufficient_evidence", never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from nirantar.llm.gateway import LLMGateway, LLMUnavailable
from nirantar.rag.store import Hit


class Citation(BaseModel):
    chunk_id: str
    quote: str = Field(min_length=8, max_length=400)


class LLMAnswer(BaseModel):
    answer: str
    citations: list[Citation]
    insufficient_evidence: bool = False


@dataclass(frozen=True)
class GroundedAnswer:
    status: Literal["answered", "insufficient_evidence", "llm_unavailable"]
    answer: str
    citations: list[dict[str, str]]
    rejected_citations: list[dict[str, str]]
    retrieved: list[str]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def check_citations(hits: list[Hit], citations: list[Citation]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    by_id = {h.chunk_id: h for h in hits}
    ok, bad = [], []
    for c in citations:
        h = by_id.get(c.chunk_id)
        if h is None:
            bad.append({"chunk_id": c.chunk_id, "reason": "not retrieved"})
        elif _norm(c.quote) not in _norm(h.text):
            bad.append({"chunk_id": c.chunk_id, "reason": "quote not in chunk"})
        else:
            ok.append({"chunk_id": c.chunk_id, "doc_id": h.doc_id, "quote": c.quote, "source_uri": h.source_uri})
    return ok, bad


def answer(question: str, hits: list[Hit], llm: LLMGateway, tier: str = "mid") -> GroundedAnswer:
    retrieved = [h.chunk_id for h in hits]
    if not hits:
        return GroundedAnswer("insufficient_evidence", "", [], [], retrieved)
    context = "\n\n".join(f"[chunk_id={h.chunk_id}] ({h.doc_id} · {h.section_path})\n{h.text}" for h in hits)
    try:
        parsed, _ = llm.complete_json(
            tier=tier, task="rag_answer", schema=LLMAnswer, max_tokens=900,
            system=("You answer compliance and product questions for an Indian recurring-payments platform. "
                    "Use ONLY the provided chunks. Every factual sentence must be supported by a citation whose "
                    "quote is copied VERBATIM from the chunk. If the chunks do not answer the question, set "
                    "insufficient_evidence=true and give no answer. Never use outside knowledge."),
            user=f"Question: {question}", untrusted={"retrieved_chunks": context})
    except LLMUnavailable:
        return GroundedAnswer("llm_unavailable", "", [], [], retrieved)
    ok, bad = check_citations(hits, parsed.citations)
    if parsed.insufficient_evidence or not ok:
        return GroundedAnswer("insufficient_evidence", "", ok, bad, retrieved)
    return GroundedAnswer("answered", parsed.answer, ok, bad, retrieved)
