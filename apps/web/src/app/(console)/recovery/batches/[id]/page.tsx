import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, BadgeIndianRupee, FlaskConical, Link2, Users } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { day, inr, ist, pct } from "@/lib/format";
import { Badge, Card, Mono, PageHeader, Stat, Table, Td, Tr } from "@/components/kit";
import { Button } from "@/components/ui/button";
import { BatchControls } from "./controls";

type Arm = { customers: number; recovered: number; value_minor: number };
type Inc = { incremental_recovery_rate: number; ci95: [number, number]; incremental_value_total_minor: number; significant: boolean };
type Report = {
  batch: { batch_id: string; name: string; status: string; experiment_id: string; holdout_pct: number; window_days: number;
    launched_by: string; launched_at: string; ends_at: string; stopped_by: string | null; stopped_at: string | null };
  states: Record<string, number>;
  recovered: Partial<Record<"treatment" | "holdout", Arm>>;
  statistics: { arms: Record<string, { n: number; recovery_rate: number; value_minor: number }>;
    incremental: Record<string, Inc> | null; note?: string };
  actions: { action_id: string; tool_name: string; status: string; policy_decision: string | null; policy_version: string | null;
    params_hash: string; created_at: string }[];
  items: { item_id: string; kind: string; customer_id: string; display_name: string | null; amount_minor: number; arm: string;
    action: string; state: string; last: { messages?: string[]; error?: string | null } | null; recovered: boolean; value_minor: number }[];
  audit: { valid: boolean; records?: number; error?: string };
};

export default async function BatchPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let r: Report;
  try {
    r = await api<Report>(`/v1/recovery/batches/${encodeURIComponent(id)}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }
  const b = r.batch;
  const t = r.recovered.treatment ?? { customers: 0, recovered: 0, value_minor: 0 };
  const h = r.recovered.holdout ?? { customers: 0, recovered: 0, value_minor: 0 };
  const rate = (a: Arm) => (a.customers ? a.recovered / a.customers : 0);
  const inc = r.statistics.incremental?.treatment;
  const live = b.status === "running" || b.status === "stopping";
  return (
    <>
      <PageHeader eyebrow="Recovery batch" title={b.name}
        subtitle={`Launched ${ist(b.launched_at)} by ${b.launched_by.replace(/^user:/, "")} · ${b.holdout_pct}% holdout · measured for ${b.window_days} days (until ${day(b.ends_at)})`}
        right={<>
          <Button asChild variant="ghost" size="sm"><Link href="/recovery"><ArrowLeft />Queue</Link></Button>
          <BatchControls batchId={b.batch_id} running={b.status === "running"} />
        </>} />

      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <Badge>{b.status}</Badge>
        {live && <span className="text-muted-foreground">Numbers update as verified payments arrive.</span>}
        {b.stopped_at && <span className="text-muted-foreground">Stopped {ist(b.stopped_at)} by {b.stopped_by?.replace(/^user:/, "")}</span>}
        <Badge tone={r.audit.valid ? "good" : "bad"}>{r.audit.valid ? `audit chain intact · ${r.audit.records} records` : "audit chain broken"}</Badge>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat emphasis label="Recovered (verified)" icon={<BadgeIndianRupee />} value={inr(t.value_minor)}
          hint={`${t.recovered} of ${t.customers} contacted-arm customers paid`} />
        <Stat label="Holdout recovered by itself" icon={<Users />} value={inr(h.value_minor)}
          hint={`${h.recovered} of ${h.customers} customers left alone`} />
        <Stat label="Uplift over holdout" icon={<FlaskConical />}
          value={inc ? `${inc.incremental_recovery_rate >= 0 ? "+" : ""}${pct(inc.incremental_recovery_rate)}` : `${pct(rate(t) - rate(h))}`}
          hint={inc ? `95% CI ${pct(inc.ci95[0])} to ${pct(inc.ci95[1])}` : (r.statistics.note ?? "too few customers for a confidence interval")} />
        <Stat label="Actions taken" icon={<Link2 />} value={r.actions.length}
          hint={Object.entries(r.states).map(([k, v]) => `${v} ${k.replaceAll("_", " ")}`).join(" · ")} />
      </div>

      <Card title="Treatment vs holdout" description="Share of customers whose payment the provider confirmed after launch." className="mt-6">
        <div className="space-y-4">
          {([["Contacted (treatment)", t, 1], ["Left alone (holdout)", h, 2]] as const).map(([label, a, s]) => (
            <div key={label}>
              <div className="mb-1 flex items-center justify-between text-sm">
                <span className="font-medium">{label}</span>
                <span className="tabular-nums text-muted-foreground">{pct(rate(a))} · {a.recovered}/{a.customers} · {inr(a.value_minor)}</span>
              </div>
              <div className="h-2 rounded-full bg-muted" role="img" aria-label={`${label}: ${pct(rate(a))}`}>
                <div className="h-2 rounded-full" style={{ width: `${Math.max(rate(a) * 100, a.customers ? 1 : 0)}%`, background: `var(--series-${s})` }} />
              </div>
            </div>
          ))}
          {inc && <p className="text-sm text-muted-foreground">
            {inc.significant ? <Badge tone="good">significant</Badge> : <Badge tone="warn">not yet significant</Badge>}{" "}
            ≈ {inr(inc.incremental_value_total_minor)} recovered because of this batch, beyond what would have come back anyway.
          </p>}
        </div>
      </Card>

      <Card title="Customers in this batch" className="mt-6">
        <Table head={["Customer", "Amount", "Arm", "What happened", "Outcome"]}>
          {r.items.map((x) => (
            <Tr key={x.item_id}>
              <Td><Link href={`/customers/${x.customer_id}`} className="font-medium hover:underline">{x.display_name ?? "Customer"}</Link></Td>
              <Td className="tabular-nums">{inr(x.amount_minor)}</Td>
              <Td><Badge tone={x.arm === "holdout" ? "neutral" : "info"}>{x.arm}</Badge></Td>
              <Td wrap><Badge>{x.state.replaceAll("_", " ")}</Badge>
                {x.last?.messages?.length ? <div className="mt-1 text-xs text-muted-foreground">{x.last.messages.join(" · ")}</div> : null}
                {x.last?.error ? <div className="mt-1 text-xs text-danger">{x.last.error}</div> : null}</Td>
              <Td>{x.recovered ? <Badge tone="good">paid {inr(x.value_minor)}</Badge> : <span className="text-muted-foreground">not yet</span>}</Td>
            </Tr>
          ))}
        </Table>
      </Card>

      <Card title="Action trail" description="Every gateway action, with the policy decision and the hash of its parameters (the audit chain covers each)." className="mt-6">
        {r.actions.length === 0 ? <p className="text-sm text-muted-foreground">No actions yet.</p> : (
          <Table head={["When", "Tool", "Status", "Policy", "Params hash"]}>
            {r.actions.map((a) => (
              <Tr key={a.action_id}>
                <Td className="text-xs text-muted-foreground">{ist(a.created_at)}</Td>
                <Td><Mono>{a.tool_name}</Mono></Td>
                <Td><Badge>{a.status}</Badge></Td>
                <Td>{a.policy_decision ? <Badge>{a.policy_decision.toLowerCase()}</Badge> : <span className="text-muted-foreground">—</span>}</Td>
                <Td><Mono>{a.params_hash.slice(0, 12)}…</Mono></Td>
              </Tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
