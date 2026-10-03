import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Mono, PageHeader, Table, Td } from "@/components/ui";

type Rec = { seq: number; at: string; actor: string; action: string; data_hash: string; hash: string };

export default async function AuditPage() {
  const [verify, recs] = await Promise.all([
    api<{ valid: boolean; records?: number; error?: string }>("/v1/audit/verify"),
    api<{ items: Rec[] }>("/v1/audit?limit=100"),
  ]);
  return (
    <>
      <PageHeader title="Audit" subtitle="Hash-chained, append-only log of every autonomous action (no raw PII)"
        right={<Badge tone={verify.valid ? "good" : "bad"}>{verify.valid ? `chain intact · ${verify.records} records` : `chain broken: ${verify.error}`}</Badge>} />
      <Card>
        <Table head={["#", "When", "Actor", "Action", "Data hash", "Record hash"]}>
          {recs.items.map((r) => (
            <tr key={r.seq}>
              <Td className="tabular-nums">{r.seq}</Td>
              <Td className="whitespace-nowrap text-xs text-[var(--muted)]">{ist(r.at)}</Td>
              <Td className="text-xs">{r.actor}</Td>
              <Td><Badge tone="neutral">{r.action}</Badge></Td>
              <Td><Mono>{r.data_hash.slice(0, 12)}…</Mono></Td>
              <Td><Mono>{r.hash.slice(0, 12)}…</Mono></Td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
