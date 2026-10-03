"""Ingestion and hybrid retrieval.

Retrieval = dense (pgvector cosine) + lexical (Postgres full-text, ts_rank_cd) → reciprocal-rank fusion →
metadata filters; optional cross-encoder rerank (off by default: it lowered MRR 0.963→0.944 on the policy
set, evals/results/rag_latest.json). Tenant isolation is enforced by RLS: a tenant connection sees
its own documents plus the shared `ten_global` corpus and nothing else.
Note: Postgres full-text ranking is not BM25; it plays the same "exact term" role in the hybrid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from nirantar.core.ids import new_id
from nirantar.rag import EMBED_MODEL, RERANK_MODEL
from nirantar.rag.corpus import Document

RRF_K = 60


@lru_cache(maxsize=1)
def _embedder() -> Any:
    from fastembed import TextEmbedding

    return TextEmbedding(EMBED_MODEL)


@lru_cache(maxsize=1)
def _reranker() -> Any:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(RERANK_MODEL)


def embed(texts: list[str], query: bool = False) -> list[list[float]]:
    model = _embedder()
    vectors = model.query_embed(texts) if query else model.passage_embed(texts)
    return [list(map(float, v)) for v in vectors]


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def ingest(conn: Connection, tenant_id: str, docs: list[Document]) -> dict[str, int]:
    """Idempotent: a document whose content hash is unchanged is skipped; changed content replaces chunks
    by writing a new versioned doc row (old versions remain for audit, retrieval uses the latest)."""
    added = skipped = chunks = 0
    for d in docs:
        existing = conn.execute(text("SELECT sha256 FROM ai.documents WHERE tenant_id=:t AND doc_id=:d"),
                                {"t": tenant_id, "d": d.doc_id}).scalar_one_or_none()
        if existing == d.sha256:
            skipped += 1
            continue
        if existing is not None:
            conn.execute(text("DELETE FROM ai.chunks WHERE tenant_id=:t AND doc_id=:d"),
                         {"t": tenant_id, "d": d.doc_id})
            conn.execute(text("DELETE FROM ai.documents WHERE tenant_id=:t AND doc_id=:d"),
                         {"t": tenant_id, "d": d.doc_id})
        conn.execute(
            text("INSERT INTO ai.documents (tenant_id, doc_id, title, document_type, regulation, jurisdiction, rail, "
                 "effective_date, version, source_uri, sha256) VALUES (:t, :d, :ti, :ty, :r, :j, :ra, :e, :v, :s, :h)"),
            {"t": tenant_id, "d": d.doc_id, "ti": d.title[:500], "ty": d.document_type, "r": d.regulation,
             "j": d.jurisdiction, "ra": d.rail, "e": d.effective_date, "v": d.version, "s": d.source_uri,
             "h": d.sha256})
        vectors = embed([f"{d.title}\n{c.section_path}\n{c.text}" for c in d.chunks])
        for c, v in zip(d.chunks, vectors, strict=True):
            conn.execute(
                text("INSERT INTO ai.chunks (tenant_id, chunk_id, doc_id, section_path, text, embedding) "
                     "VALUES (:t, :c, :d, :sp, :tx, CAST(:e AS vector))"),
                {"t": tenant_id, "c": new_id("chk"), "d": d.doc_id, "sp": c.section_path[:500], "tx": c.text,
                 "e": _vec(v)})
            chunks += 1
        added += 1
    return {"documents_added": added, "documents_unchanged": skipped, "chunks_added": chunks}


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    document_type: str
    section_path: str
    text: str
    source_uri: str
    effective_date: date | None
    score: float
    fused_rank: int


def retrieve(conn: Connection, query: str, *, k: int = 5, candidates: int = 30,
             document_types: list[str] | None = None, as_of: date | None = None, rerank: bool = False) -> list[Hit]:
    qv = _vec(embed([query], query=True)[0])
    filters, params = "", {"qv": qv, "q": query, "n": candidates}
    if document_types:
        filters += " AND d.document_type = ANY(:types)"
        params["types"] = document_types
    if as_of:
        filters += " AND (d.effective_date IS NULL OR d.effective_date <= :asof)"
        params["asof"] = as_of
    base = ("FROM ai.chunks c JOIN ai.documents d ON d.tenant_id=c.tenant_id AND d.doc_id=c.doc_id "
            f"WHERE true{filters}")
    dense: list[str] = list(conn.execute(
        text(f"SELECT c.chunk_id {base} ORDER BY c.embedding <=> CAST(:qv AS vector) LIMIT :n"),
        params).scalars().all())
    lexical: list[str] = list(conn.execute(text(
        f"SELECT c.chunk_id {base} AND c.tsv @@ websearch_to_tsquery('english', :q) "
        "ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) DESC LIMIT :n"), params).scalars().all())
    fused: dict[str, float] = {}
    for ranking in (dense, lexical):
        for rank, cid in enumerate(ranking):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    order = sorted(fused, key=lambda c: -fused[c])[:candidates]
    if not order:
        return []
    rows = {r.chunk_id: r for r in conn.execute(text(
        "SELECT c.chunk_id, c.doc_id, d.title, d.document_type, c.section_path, c.text, d.source_uri, "
        "d.effective_date FROM ai.chunks c JOIN ai.documents d ON d.tenant_id=c.tenant_id AND d.doc_id=c.doc_id "
        "WHERE c.chunk_id = ANY(:ids)"), {"ids": order}).all()}
    scores = {cid: fused[cid] for cid in order}
    if rerank:
        ce = list(_reranker().rerank(query, [f"{rows[c].title}\n{rows[c].text}" for c in order]))
        scores = {cid: float(s) for cid, s in zip(order, ce, strict=True)}
    ranked = sorted(order, key=lambda c: -scores[c])[:k]
    return [Hit(c, rows[c].doc_id, rows[c].title, rows[c].document_type, rows[c].section_path, rows[c].text,
                rows[c].source_uri, rows[c].effective_date, scores[c], order.index(c) + 1) for c in ranked]


def ingest_global(owner_engine: Engine, docs: list[Document]) -> dict[str, int]:
    """Shared corpus is written by the platform (owner) role only; tenants can read but never write it."""
    from nirantar.rag import GLOBAL_TENANT

    with owner_engine.begin() as c:
        return ingest(c, GLOBAL_TENANT, docs)


def hits_json(hits: list[Hit]) -> str:
    return json.dumps([{"chunk_id": h.chunk_id, "doc_id": h.doc_id, "score": round(h.score, 4)} for h in hits])
