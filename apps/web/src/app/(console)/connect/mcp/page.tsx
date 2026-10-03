import { api, ApiError } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader, Table, Td } from "@/components/ui";
import { ConsentForm, RevokeButton } from "./forms";

type Request = { request_id: string; client_name: string; client_id: string; redirect_uri: string; scopes: string[];
  expires_at: string };
type Grant = { grant_id: string; client_id: string; client_name: string | null; principal_id: string; scopes: string[];
  created_at: string; revoked_at: string | null; revoked_reason: string | null; last_token_at: string | null };

const MCP_URL = process.env.NIRANTAR_MCP_URL ?? "http://localhost:18100";

async function loadRequest(id: string): Promise<{ req?: Request; error?: string }> {
  try {
    return { req: await api<Request>(`/v1/mcp/requests/${encodeURIComponent(id)}`) };
  } catch (e) {
    return { error: e instanceof ApiError && e.status === 404 ? e.message.replace(/^404 /, "") : "Could not load this request" };
  }
}

export default async function ConnectMcpPage({ searchParams }: { searchParams: Promise<{ request?: string }> }) {
  const { request } = await searchParams;
  const pending = request ? await loadRequest(request) : null;
  const grants = await api<{ items: Grant[] }>("/v1/mcp/grants");
  return (
    <>
      <PageHeader title="Connected AI apps"
        subtitle="Let Claude or any MCP client work with your Nirantar account. Every call goes through your policies, approvals and audit trail." />
      <div className="space-y-4">
        {pending?.req && (
          <Card title={`${pending.req.client_name} wants to connect`}>
            <p className="mb-3 text-sm text-[var(--muted)]">
              After you connect, it returns to <Mono>{new URL(pending.req.redirect_uri).host}</Mono>. This request expires at {ist(pending.req.expires_at)}.
            </p>
            <ConsentForm requestId={pending.req.request_id} scopes={pending.req.scopes} />
          </Card>
        )}
        {pending?.error && <Card title="Connection request"><p className="text-sm text-[var(--bad-fg)]">{pending.error}</p></Card>}
        <Card title="How to connect">
          <p className="text-sm">Add this server address to your MCP client (for example Claude → Settings → Connectors):</p>
          <p className="mt-2"><Mono>{`${MCP_URL}/mcp`}</Mono></p>
          <p className="mt-2 text-xs text-[var(--muted)]">The client opens this page so you can choose what it may do. Money actions always wait for your approval.</p>
        </Card>
        <Card title="Connections">
          {grants.items.length === 0 ? <Empty title="No AI apps connected yet" /> : (
            <Table head={["App", "Permissions", "Connected", "Last token", "Status", ""]}>
              {grants.items.map((g) => (
                <tr key={g.grant_id}>
                  <Td>{g.client_name ?? <Mono>{g.client_id}</Mono>}</Td>
                  <Td>{g.scopes.map((s) => <Badge key={s}>{s.replace("nirantar:", "")}</Badge>)}</Td>
                  <Td>{ist(g.created_at)}</Td>
                  <Td>{ist(g.last_token_at)}</Td>
                  <Td>{g.revoked_at ? <Badge tone="bad">revoked</Badge> : <Badge tone="good">active</Badge>}</Td>
                  <Td>{!g.revoked_at && <RevokeButton grantId={g.grant_id} />}</Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </div>
    </>
  );
}
