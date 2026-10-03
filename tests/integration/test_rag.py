from __future__ import annotations

import os

import pytest
from sqlalchemy import Engine

from nirantar.billing.service import create_tenant
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.llm.gateway import LLMGateway, StubProvider
from nirantar.rag.answer import Citation, answer, check_citations
from nirantar.rag.corpus import Chunk, Document, chunk_markdown, global_corpus
from nirantar.rag.store import ingest, ingest_global, retrieve

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def corpus_ready(owner_engine: Engine) -> None:
    ingest_global(owner_engine, global_corpus())


def _tenant(engine: Engine) -> str:
    t = new_id("ten")
    with tenant_tx(t, engine) as c:
        create_tenant(c, t, "x")
    return t


def test_chunker_keeps_heading_paths() -> None:
    chunks = chunk_markdown("# A\nintro\n## B\nbody b\n### C\nbody c\n## D\nbody d")
    assert [c.section_path for c in chunks] == ["A", "A > B", "A > B > C", "A > D"]


def test_retrieval_finds_the_right_policy(app_engine: Engine, corpus_ready: None) -> None:
    t = _tenant(app_engine)
    with tenant_tx(t, app_engine) as c:
        hits = retrieve(c, "pre-debit notification hours before debit", k=5)
    assert hits[0].doc_id == "IN-RBI-EMANDATE-PREDEBIT-001"


def test_tenant_documents_are_isolated(app_engine: Engine, corpus_ready: None) -> None:
    a, b = _tenant(app_engine), _tenant(app_engine)
    doc = Document(doc_id="tenant-refund-policy", title="Chai Club refund policy", document_type="tenant_policy",
                   source_uri="tenant://a/refunds", version="1",
                   chunks=[Chunk("Refunds", "Chai Club refunds unused subscription days within 14 days of renewal.")])
    with tenant_tx(a, app_engine) as c:
        ingest(c, a, [doc])
        assert any(h.doc_id == "tenant-refund-policy" for h in retrieve(c, "refund unused subscription days", k=5))
    with tenant_tx(b, app_engine) as c:
        assert all(h.doc_id != "tenant-refund-policy" for h in retrieve(c, "refund unused subscription days", k=5))
    # re-ingesting a CHANGED tenant document replaces its chunks (needs tenant-scoped DELETE, migration 0006)
    v2 = Document(doc_id="tenant-refund-policy", title="Chai Club refund policy", document_type="tenant_policy",
                  source_uri="tenant://a/refunds", version="2",
                  chunks=[Chunk("Refunds", "Chai Club refunds unused subscription days within 30 days of renewal.")])
    with tenant_tx(a, app_engine) as c:
        assert ingest(c, a, [v2])["documents_added"] == 1
        texts = [h.text for h in retrieve(c, "refund unused subscription days", k=5)
                 if h.doc_id == "tenant-refund-policy"]
        assert texts and all("30 days" in t for t in texts)
    # a tenant can never delete from the shared corpus
    with tenant_tx(a, app_engine) as c:
        from sqlalchemy import text as sql
        gone = c.execute(sql("DELETE FROM ai.chunks WHERE tenant_id='ten_global'")).rowcount
        assert gone == 0


def test_citation_checker_and_fail_closed(app_engine: Engine, corpus_ready: None) -> None:
    t = _tenant(app_engine)
    with tenant_tx(t, app_engine) as c:
        hits = retrieve(c, "recovery agent contact hours", k=5)
    real = hits[0]
    ok, bad = check_citations(hits, [Citation(chunk_id=real.chunk_id, quote=real.text[20:80]),
                                     Citation(chunk_id="chk_fabricated", quote="anything at all here"),
                                     Citation(chunk_id=real.chunk_id, quote="calls allowed at midnight always")])
    assert len(ok) == 1 and {b["reason"] for b in bad} == {"not retrieved", "quote not in chunk"}
    fabricating = LLMGateway({"groq": StubProvider(lambda _m: '{"answer":"Anytime is fine.","citations":'
                                                               '[{"chunk_id":"chk_x","quote":"calls allowed anytime"}]}',
                                                   "groq")})
    result = answer("When can agents call?", hits, fabricating)
    assert result.status == "insufficient_evidence" and result.rejected_citations


@pytest.mark.sandbox
def test_live_grounded_answer(app_engine: Engine, corpus_ready: None) -> None:
    if not os.environ.get("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY not set")
    t = _tenant(app_engine)
    with tenant_tx(t, app_engine) as c:
        hits = retrieve(c, "Between what hours may a lender's recovery agent contact a borrower?", k=5)
    result = answer("Between what hours may a lender's recovery agent contact a borrower?", hits,
                    LLMGateway.from_env())
    print(result.status, result.answer, result.citations)
    assert result.status == "answered"
    assert any(c["doc_id"].startswith("IN-RBI-RBC-RECOVERY") for c in result.citations)
