# ADR-0016: Real protocols: MCP server with OAuth 2.1, and A2A v1.0 (Phase 2 · P7)

- Status: Accepted · Date: 2026-10-02
- Evidence:
  - `tests/e2e/test_mcp_server.py`: the real server over HTTP, the SDK's own MCP client, the full OAuth flow, and
    an approval executed after consent.
  - `tests/e2e/test_a2a_server.py`: the official `a2a-sdk` client over HTTP.
  - `tests/tenancy` (RLS on the new tables).
  - A browser check of the dashboard consent page: approve → redirect with code and state → connection listed →
    disconnect → revoked.

## Context

The tool gateway (scope → schema → policy → approval → execute → audit) existed, but only Nirantar's own agents could
use it. Merchants want their own assistant (Claude or another MCP client) to work with their account. Customers'
own AI agents and partner agents need a standard way to ask for things. The earlier A2A module was a custom signed
message format, not the A2A specification.

## Decisions

1. **MCP server**, built on the official Python SDK (`mcp` 2.2, `MCPServer`, streamable HTTP), runs as its own
   process (`python -m nirantar.mcp`, port 18100).
   - Every tool call goes through the SAME `ToolGateway`. The calling agent is recorded as `mcp_client:<client_id>`.
   - Exposure is by OAuth scope:

     | Scope | Tools |
     |---|---|
     | `nirantar:read` | overview, open cases, profile, policy dry-run, templates, ledger verification |
     | `nirantar:act` | messages, case updates, mandate repair, win-back |
     | `nirantar:money` | payment links, representments, discount offers — **always** held for human approval, whatever the amount |

   - Never exposed:
     - experiment assignment, which would bias the holdout;
     - the mandatory pre-debit notice, which the workflow owns;
     - treasury, which is gated.
   - Refusals come back as tool errors with a reason (the SDK's `ToolError`), not as generic crashes.
2. **OAuth 2.1 authorization server** in Postgres, implementing the SDK's provider protocol.
   - Clients register dynamically (RFC 7591). PKCE is required.
   - **Consent happens in the merchant's dashboard** ("Connected AI apps"), where the merchant is already signed
     in. No secret is ever typed into an OAuth page. Only a tenant admin can approve, and they choose the scopes.
   - Codes (5 min) and refresh tokens are single use. Access tokens last 1 h.
   - **Refresh rotation with reuse detection:** presenting an already-used refresh token revokes the whole grant.
   - Token plaintext carries the tenant (`nat_<tenant>.…`, like API keys), so lookups run under RLS. Only an HMAC
     hash is stored.
   - Merchants list and revoke connections, and every connection and revocation is audited.
   - Not supported yet: enterprise identity assertions (SEP-990). The provider rejects them until OIDC arrives in P8.
3. **A2A v1.0** on the official `a2a-sdk` 1.2.
   - JSON-RPC binding at `POST /a2a` on the API.
   - Agent Card at `/.well-known/agent-card.json`, plus a per-merchant card at `/a2a/tenants/{id}/agent-card.json`
     (`supportedInterfaces[].tenant`).
   - `A2A-Version: 1.0` is negotiated. Our first context builder dropped the request headers, so every call looked
     like version 0.3. The fix extends the SDK's default builder.
   - **Callers** are A2A partners (e.g. a customer's own AI agent) that the merchant registers.
     - Each gets a bearer key (HTTP bearer security scheme) bound to one tenant, with role `a2a_partner`.
     - That role can do nothing except call A2A.
     - The tenant comes from authentication, never from the request.
     - A partner sees only its own tasks. Another partner gets `TaskNotFound`, so a task's existence is not revealed.
   - **Skills:**
     - `subscription.status`: needs the customer reference and a consent reference, both recorded.
     - `subscription.pause` (1–3 months): always waits for the merchant's approval. The task stays WORKING;
       `GetTask` reflects the decision (COMPLETED with the result, or FAILED).
     - A missing field → INPUT_REQUIRED. An unknown skill → REJECTED.
   - **Persistence:** tasks persist in `ops.a2a_tasks` under RLS (migration 0018). Linking a task to its approval
     is an upsert, because the SDK may save the task after the executor runs. That was a real race, caught by the
     test.
   - The older signed-message library stays for outbound partner flows (collections handoff), which remain legally
     gated.
4. **Approved actions run with the proposer's own tool set**, through `approvals.executor.ApprovalExecutor`.
   Tenant provider accounts are resolved per tenant; messages use the deployment's real channel, or fail loudly.
   - This fixed a pre-existing gap: the API server (`api/serve.py`) executed approved actions with a
     **MockProvider and MockCommsSink**, a leftover from the first demo.
   - Now only tenants whose connected account *is* the simulator (seeded demo tenants) get one, and never in
     production.
5. **Customer-delegated pause** (`subscription.request_pause`) pauses at the provider, marks the subscription
   paused, and cancels scheduled debits inside the window (30-day months).

## Consequences / limits

- **No resume at the end of a pause.** The provider does not auto-resume, and Nirantar does not schedule the resume
  yet; it is an explicit later action. The A2A result states the `paused_until` date.
- **MCP tool listing shows all exposed tools.** The scope check happens at call time; each description says which
  scope it needs.
- **Local only.** Nothing here has been exposed on the internet: the MCP issuer is `http://localhost:18100`, and the
  public URLs are configured with `NIRANTAR_MCP_URL` / `NIRANTAR_PUBLIC_URL` at deployment (P8).
- **Not yet tested with real clients.** Claude's own connector and third-party A2A agents have not connected yet;
  they need a public HTTPS URL (P8). Interoperability is shown with each protocol's official client library.
