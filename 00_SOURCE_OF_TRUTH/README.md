# 00_SOURCE_OF_TRUTH

Functional baseline for Nirantar. Everything in the repo traces back to a file here.

| File | What it holds | Status |
|---|---|---|
| `01-product-spec.md` | Product mission, customers, loops, positioning | Baseline v1.0 |
| `02-architecture-spec.md` | Models, agents, MCP servers, A2A flows, voice pipeline, governance | Baseline v1.0 |
| `03-build-brief.md` | Engineering brief (standards, phases, definition of done) supplied by the product owner | Baseline v1.0 |
| `04-research-notes.md` | Market and competitor research with sources, retrieved 2026-09-28 | Snapshot, re-verify before relying |
| `05-decision-log.md` | Decisions taken during product discovery, with reasons | Living |
| `diagrams/` | The 8 architecture images (01-overview … 08-sarvam-voice-ai) | **Pending**: product owner is generating and approving them |

## Provenance

These documents were written on 2026-09-28 from the product-discovery conversation between the product owner and Claude. The diagrams are specified in `02-architecture-spec.md §12` and will be added to `diagrams/` once approved.

## Rules

1. Time-sensitive facts here (regulations, provider APIs, model IDs, market figures) are **snapshots**. Verified, dated versions live in `docs/compliance/` and `docs/integrations/`. When they disagree, the verified doc wins and an ADR records the change.
2. Nothing in this folder is edited silently. Changes go through an ADR in `docs/adr/`.
