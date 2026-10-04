import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader, Table, Td, Tr } from "@/components/kit";

type Decision = { action_id: string; agent_id: string; tool_name: string; policy_decision: string; policy_version: string; created_at: string };
type Policy = { policy_id: string; rule: string | null; effective_date: string | null; status: string; source: string | null };

export default async function CompliancePage() {
  const [c, p] = await Promise.all([
    api<{ decisions: Decision[]; contact_log: { channel: string; purpose: string; status: string; n: number }[] }>("/v1/compliance?days=60"),
    api<{ items: Policy[] }>("/v1/compliance/policies"),
  ]);
  return (
    <>
      <PageHeader title="Compliance" subtitle="Every non-ALLOW decision, the contact log, and the versioned policy knowledge base (engineering view, not legal advice)" />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Guardian decisions (last 60 days)">
          {c.decisions.length === 0 ? <Empty title="No blocked or escalated actions" /> : (
            <Table head={["When", "Agent", "Tool", "Decision"]}>
              {c.decisions.slice(0, 50).map((d) => (
                <Tr key={d.action_id}>
                  <Td className="text-xs text-muted-foreground">{ist(d.created_at)}</Td>
                  <Td>{d.agent_id}</Td><Td><Mono>{d.tool_name}</Mono></Td><Td><Badge>{d.policy_decision}</Badge></Td>
                </Tr>
              ))}
            </Table>
          )}
        </Card>
        <Card title="Contact log">
          {c.contact_log.length === 0 ? <Empty title="No customer contacts" /> : (
            <Table head={["Channel", "Purpose", "Status", "Count"]}>
              {c.contact_log.map((r, i) => (
                <Tr key={i}><Td>{r.channel}</Td><Td>{r.purpose}</Td><Td><Badge>{r.status}</Badge></Td><Td className="tabular-nums">{r.n}</Td></Tr>
              ))}
            </Table>
          )}
        </Card>
      </div>
      <Card title={`Policy knowledge base (${p.items.length} records)`} className="mt-6">
        <Table head={["Policy", "Rule", "Effective", "Verification"]}>
          {p.items.map((r) => (
            <Tr key={r.policy_id}>
              <Td><Mono>{r.policy_id}</Mono></Td>
              <Td wrap className="max-w-xl text-xs">{r.rule}</Td>
              <Td className="whitespace-nowrap text-xs">{r.effective_date ?? "—"}</Td>
              <Td><Badge tone={r.status === "verified" ? "good" : r.status === "governance" ? "info" : "warn"}>{r.status}</Badge></Td>
            </Tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
