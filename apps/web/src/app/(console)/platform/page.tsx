import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader, Stat, Table, Td, Tr } from "@/components/kit";

type Health = {
  tenants: number; outbox_unpublished: number; outbox_oldest_unpublished_s: number;
  provider_events: Record<string, number>; dead_letter_events: number; pending_approvals: number;
  failed_actions_24h: number; open_discrepancies: number;
};
type Tenant = { tenant_id: string; name: string; status: string; created_at: string; customers: number; debits: number; actions: number };
type Dead = { tenant_id: string; raw_event_id: string; provider: string; event_type: string; attempts: number; last_error: string | null; received_at: string };

export default async function PlatformPage() {
  const [h, t, d] = await Promise.all([
    api<Health>("/platform/health", { kind: "platform" }),
    api<{ items: Tenant[] }>("/platform/tenants", { kind: "platform" }),
    api<{ items: Dead[] }>("/platform/dead-letters", { kind: "platform" }),
  ]);
  return (
    <>
      <PageHeader title="Platform console" subtitle="Cross-tenant operational health for the Nirantar operator (counts only — no tenant data contents)" />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Tenants" value={h.tenants} />
        <Stat label="Outbox backlog" value={h.outbox_unpublished} hint={`oldest ${Math.round(h.outbox_oldest_unpublished_s)} s`} />
        <Stat label="Dead-letter webhooks" value={h.dead_letter_events} />
        <Stat label="Failed actions (24h)" value={h.failed_actions_24h} hint={`${h.open_discrepancies} open discrepancies`} />
      </div>
      <Card title="Provider events by status" className="mt-6">
        <div className="flex flex-wrap gap-3">{Object.entries(h.provider_events).map(([s, n]) => (<span key={s} className="text-sm"><Badge>{s}</Badge> <span className="tabular-nums">{n}</span></span>))}</div>
      </Card>
      <Card title="Tenants" className="mt-6">
        <Table head={["Tenant", "Name", "Status", "Customers", "Debits", "Agent actions", "Created"]}>
          {t.items.map((x) => (
            <Tr key={x.tenant_id}><Td><Mono>{x.tenant_id.slice(0, 14)}…</Mono></Td><Td>{x.name}</Td><Td><Badge>{x.status === "active" ? "ok" : x.status}</Badge></Td>
              <Td className="tabular-nums">{x.customers}</Td><Td className="tabular-nums">{x.debits}</Td><Td className="tabular-nums">{x.actions}</Td>
              <Td className="text-xs text-muted-foreground">{ist(x.created_at)}</Td></Tr>
          ))}
        </Table>
      </Card>
      <Card title="Dead-letter queue" className="mt-6">
        {d.items.length === 0 ? <Empty title="No dead-lettered provider events" /> : (
          <Table head={["Received", "Provider", "Event", "Attempts", "Last error"]}>
            {d.items.map((x) => (<Tr key={x.raw_event_id}><Td className="text-xs">{ist(x.received_at)}</Td><Td>{x.provider}</Td><Td>{x.event_type}</Td><Td>{x.attempts}</Td><Td wrap className="text-xs">{x.last_error}</Td></Tr>))}
          </Table>
        )}
      </Card>
    </>
  );
}
