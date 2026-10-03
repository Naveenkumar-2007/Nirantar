import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader } from "@/components/ui";
import { DecideButtons } from "./decide-buttons";

type Approval = {
  approval_id: string; action_id: string; reason: string; status: string; requested_at: string; requested_by: string;
  tool_name: string; expires_at: string; decided_by: string | null; decided_at: string | null;
  params: Record<string, unknown> | null; case_id: string | null;
};

export default async function ApprovalsPage() {
  const [pending, done] = await Promise.all([
    api<{ items: Approval[] }>("/v1/approvals?status=pending"),
    api<{ items: Approval[] }>("/v1/approvals?status=used"),
  ]);
  return (
    <>
      <PageHeader title="Approvals" subtitle="Maker-checker: agents propose, a human with approvals:decide approves. Tokens are scoped, expiring and single-use." />
      <div className="space-y-4">
        {pending.items.length === 0 && <Empty title="Nothing waiting for you" hint="Agents will queue money actions above your thresholds here." />}
        {pending.items.map((a) => (
          <Card key={a.approval_id}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2"><Mono>{a.tool_name}</Mono><Badge>{a.status}</Badge></div>
                <p className="mt-2 text-sm">{a.reason}</p>
                <p className="mt-1 text-xs text-[var(--muted)]">Requested by {a.requested_by} · {ist(a.requested_at)} · expires {ist(a.expires_at)}</p>
                <pre className="mt-2 max-w-xl overflow-auto rounded bg-[var(--chip)] p-2 text-xs">{JSON.stringify(a.params, null, 2)}</pre>
              </div>
              <DecideButtons approvalId={a.approval_id} />
            </div>
          </Card>
        ))}
        {done.items.length > 0 && (
          <Card title="Recently executed after approval">
            <ul className="space-y-1 text-sm">
              {done.items.slice(0, 10).map((a) => (
                <li key={a.approval_id}><Mono>{a.tool_name}</Mono> · approved by {a.decided_by} · {ist(a.decided_at)}</li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    </>
  );
}
