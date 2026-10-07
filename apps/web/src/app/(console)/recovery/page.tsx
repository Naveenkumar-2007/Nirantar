import Link from "next/link";
import { BadgeIndianRupee, Hourglass, ShieldAlert, Target } from "lucide-react";
import { api } from "@/lib/api";
import { day, inr, pct } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat, Table, Td, Tr } from "@/components/kit";
import { Workbench, type QueueItem } from "./workbench";

type Queue = {
  generated_at: string; items: QueueItem[];
  totals: Record<string, { n: number; amount_minor: number; expected_minor: number }>;
  history: { failed_debits: number; overall_recovery_rate: number | null };
};
type Batch = { batch_id: string; name: string; status: string; holdout_pct: number; window_days: number;
  launched_by: string; launched_at: string; ends_at: string; items: number; executed: number; amount_minor: number };

export const metadata = { title: "Recovery" };

export default async function RecoveryPage() {
  const [q, b] = await Promise.all([api<Queue>("/v1/recovery/queue"), api<{ items: Batch[] }>("/v1/recovery/batches")]);
  const atRisk = q.items.reduce((s, x) => s + x.amount_minor, 0);
  const expected = q.items.reduce((s, x) => s + (x.expected_minor ?? 0), 0);
  const stalled = q.items.filter((x) => ["stalled", "promise_broken", "blocked"].includes(x.state)).length;
  const waiting = q.items.filter((x) => x.state === "needs_approval").length;
  return (
    <>
      <PageHeader eyebrow="Operate" title="Recovery"
        subtitle="Every rupee at risk, why it is stuck, and the next best action. Run a batch: preview every message and its compliance check, keep a random holdout, and get verified proof of what it recovered." />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat emphasis label="Money at risk" icon={<BadgeIndianRupee />} value={inr(atRisk)}
          hint={`${q.items.length} items · failed, at-risk and mandate problems`} />
        <Stat label="Expected recoverable" icon={<Target />} value={q.history.overall_recovery_rate === null ? "—" : inr(expected)}
          hint={q.history.overall_recovery_rate === null ? "no settled recovery history yet"
            : `from ${q.history.failed_debits} settled failures · ${pct(q.history.overall_recovery_rate)} overall`} />
        <Stat label="Stalled" icon={<Hourglass />} value={stalled} hint="no recent contact, broken promise or blocked" />
        <Stat label="Waiting for a person" icon={<ShieldAlert />} value={waiting} hint={<Link href="/approvals" className="underline">open approvals</Link>} />
      </div>

      <div className="mt-6">
        {q.items.length === 0
          ? <Card><Empty icon={<BadgeIndianRupee />} title="Nothing at risk right now"
              hint="Declined debits, high-risk upcoming debits and broken mandates appear here as soon as they happen." /></Card>
          : <Workbench items={q.items} />}
      </div>

      <Card title="Batches" description="Each batch is a bounded run with its own holdout, measured on verified payments." className="mt-6">
        {b.items.length === 0 ? <Empty title="No batches yet" hint="Select items above and preview a batch." /> : (
          <Table head={["Batch", "Status", "Items", "Sent", "Holdout", "Window", "Launched"]}>
            {b.items.map((x) => (
              <Tr key={x.batch_id}>
                <Td><Link href={`/recovery/batches/${x.batch_id}`} className="font-medium text-primary hover:underline">{x.name}</Link></Td>
                <Td><Badge>{x.status}</Badge></Td>
                <Td className="tabular-nums">{x.items} · {inr(x.amount_minor)}</Td>
                <Td className="tabular-nums">{x.executed}</Td>
                <Td className="tabular-nums">{x.holdout_pct}%</Td>
                <Td>{x.window_days} days · ends {day(x.ends_at)}</Td>
                <Td className="text-xs text-muted-foreground">{day(x.launched_at)} · {x.launched_by.replace(/^user:/, "")}</Td>
              </Tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
