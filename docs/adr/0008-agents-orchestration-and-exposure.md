# ADR-0008: Agents, orchestration and experiment exposure semantics

- Status: Accepted · Date: 2026-09-28 · Traces: BB-§13–16, §21, §25, §53 · Evidence: tests/e2e/test_acceptance_999.py

## Decisions

1. **Temporal owns time; LangGraph owns a single decision.** `DebitCycleWorkflow` handles days of waiting
   (debit date, payment signals, contact windows, recovery window). The Conductor is a LangGraph graph that
   runs inside one activity and makes one round's decision. No LangGraph checkpointing for durability.
2. **Workflow time is the only clock for decisions.** Activities receive `now` from `workflow.now()` and the
   MCP gateway uses a `FixedClock` from it, so contact-window decisions are reproducible and testable.
3. **Denials are re-evaluated, not cached.** The gateway dedupes executed write actions only; a DENY (e.g.
   outside contact hours) is re-evaluated on retry. Read tools are never deduped.
4. **Deferred rounds are not exposures.** If every channel is closed *now* but opens later (`retry_after`),
   the workflow sleeps until the window and retries the round; no `experiment.exposure` is logged for the
   deferred attempt. Found by the acceptance test: logging "none" then "whatsapp" double-counts a customer.
5. **LLM output is constrained, then checked, then replaceable:** amount string must be exact, one `{link}`,
   conduct screen, and (added after the live E2E run) the draft must be in the customer's script
   (Telugu/Devanagari ≥ 50% of letters) — else the approved template is used.
6. **Indic tier = `sarvam-105b-conversations`** (ADR-0005 amendment).

## Consequences

The ₹999 scenario is reproducible in ~1 minute with time skipping, and runs with either stub or live LLMs.
Voice, disputes, collections, mandate repair and treasury agents are specified (`agents/specs.py`, status
`planned`) but not implemented yet.
