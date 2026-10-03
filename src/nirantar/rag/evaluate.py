"""RAG evaluation: retrieval recall@k / MRR on the labelled set, dense-only vs hybrid vs hybrid+rerank.

Run: uv run python -m nirantar.rag.evaluate
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine, create_engine

from nirantar.db.session import tenant_tx
from nirantar.rag.corpus import REPO, global_corpus
from nirantar.rag.store import ingest_global, retrieve

QUESTIONS = REPO / "evals" / "rag" / "questions.yaml"


def run(app_engine: Engine, owner_engine: Engine, tenant_id: str, k: int = 5) -> dict[str, Any]:
    ingest_report = ingest_global(owner_engine, global_corpus())
    qs = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    variants = {"hybrid_rrf": False, "hybrid_rrf_rerank": True}
    report: dict[str, Any] = {"ingest": ingest_report, "k": k, "n_questions": len(qs), "variants": {}}
    with tenant_tx(tenant_id, app_engine) as c:
        for name, use_rerank in variants.items():
            hits_at_k, rr, misses = 0, 0.0, []
            for item in qs:
                hits = retrieve(c, item["q"], k=k, rerank=use_rerank)
                ranks = [i for i, h in enumerate(hits, start=1) if h.doc_id in item["expect"]]
                if ranks:
                    hits_at_k += 1
                    rr += 1.0 / ranks[0]
                else:
                    misses.append({"q": item["q"], "got": [h.doc_id for h in hits[:3]]})
            report["variants"][name] = {"recall_at_k": hits_at_k / len(qs), "mrr": rr / len(qs), "misses": misses}
    return report


def main() -> None:
    import os

    app = create_engine(os.environ.get("DATABASE_URL",
                                       "postgresql+psycopg://nirantar_app:nirantar_app@localhost:25432/nirantar"))
    owner = create_engine(os.environ.get("DATABASE_OWNER_URL",
                                         "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar"))
    from nirantar.billing.service import create_tenant
    from nirantar.core.ids import new_id

    tenant = new_id("ten")
    with tenant_tx(tenant, app) as c:
        create_tenant(c, tenant, "rag-eval")
    report = run(app, owner, tenant)
    out = Path(REPO / "evals" / "results" / "rag_latest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "variants"}, default=str))
    for name, v in report["variants"].items():
        print(f"{name:<20} recall@{report['k']}={v['recall_at_k']:.3f}  MRR={v['mrr']:.3f}  misses={len(v['misses'])}")
        for m in v["misses"]:
            print("   miss:", m["q"][:70], "->", m["got"])


if __name__ == "__main__":
    main()
