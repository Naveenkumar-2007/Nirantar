"""Build the document corpus: regulatory policy records, governance policies, ADRs, provider docs.

Chunking is section-aware: markdown is split on headings (the heading path is kept as `section_path`),
long sections are split on paragraph boundaries with a small overlap. Policy records become one
document each with rich metadata (effective date, verification status) so answers can be filtered by
date and so the evaluation can check that the *right* policy was retrieved.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
MAX_CHARS = 1200
OVERLAP = 150


@dataclass(frozen=True)
class Chunk:
    section_path: str
    text: str


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    document_type: str            # regulation | governance | adr | provider_doc | tenant_policy
    source_uri: str
    version: str
    chunks: list[Chunk]
    regulation: str | None = None
    jurisdiction: str = "IN"
    rail: str | None = None
    effective_date: date | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256("\n".join(c.section_path + "\n" + c.text for c in self.chunks).encode()).hexdigest()


def split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > MAX_CHARS:
            out.append(cur)
            cur = cur[-OVERLAP:] + "\n\n" + p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
        while len(cur) > MAX_CHARS * 1.5:  # a single giant paragraph
            out.append(cur[:MAX_CHARS])
            cur = cur[MAX_CHARS - OVERLAP:]
    if cur:
        out.append(cur)
    return out


def chunk_markdown(md: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    path: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            for piece in split_long(body):
                chunks.append(Chunk(" > ".join(path) or "(root)", piece))
        buf.clear()

    for line in md.splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = [*path[: level - 1], m.group(2).strip()]
        else:
            buf.append(line)
    flush()
    return chunks


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value and str(value) != "UNVERIFIED" else None
    except ValueError:
        return None


def policy_documents() -> list[Document]:
    docs = []
    for path, kind in ((REPO / "docs/compliance/policies.yaml", "regulation"),
                       (REPO / "docs/compliance/internal-policies.yaml", "governance")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        records = data["policies"] if isinstance(data, dict) else data
        for r in records:
            src = r.get("source")
            title = src.get("title") if isinstance(src, dict) else str(src)
            url = src.get("url", "") if isinstance(src, dict) else ""
            fields = [("Rule", r.get("rule")), ("Applicability", r.get("applicability")),
                      ("Exceptions", r.get("exceptions")), ("Implementation in Nirantar", r.get("implementation")),
                      ("Test case", r.get("test_case"))]
            body = "\n\n".join(f"{k}: {v}" for k, v in fields if v)
            header = (f"{r['policy_id']} — {title}\nEffective: {r.get('effective_date', 'n/a')} · "
                      f"Status: {r.get('verification_status', 'governance')}")
            docs.append(Document(
                doc_id=r["policy_id"], title=title or r["policy_id"], document_type=kind,
                source_uri=url or path.relative_to(REPO).as_posix(), version=str(r.get("retrieved", "v1")),
                chunks=[Chunk(r["policy_id"], f"{header}\n\n{piece}") for piece in split_long(body)],
                regulation=r["policy_id"].split("-")[1] if kind == "regulation" else "NIR",
                effective_date=_parse_date(r.get("effective_date")),
                extra={"verification_status": r.get("verification_status", "governance")}))
    return docs


def markdown_documents(folder: str, kind: str) -> list[Document]:
    docs = []
    for path in sorted((REPO / folder).glob("*.md")):
        md = path.read_text(encoding="utf-8")
        title = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("#")), path.stem)
        docs.append(Document(doc_id=f"{kind}:{path.stem}", title=title, document_type=kind,
                             source_uri=path.relative_to(REPO).as_posix(), version="repo", chunks=chunk_markdown(md)))
    return docs


def global_corpus() -> list[Document]:
    return policy_documents() + markdown_documents("docs/adr", "adr") + \
        markdown_documents("docs/integrations", "provider_doc")
