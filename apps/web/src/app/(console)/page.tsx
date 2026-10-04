import Link from "next/link";
import { ArrowRight, BadgeIndianRupee, ClipboardCheck, ShieldAlert, Wallet } from "lucide-react";
import { api } from "@/lib/api";
import { inr, pct } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat } from "@/components/kit";
import { Button } from "@/components/ui/button";

type Inc = {
  incremental_recovery_rate: number; ci95: [number, number]; incremental_value_per_customer_minor: number;
  incremental_value_total_minor: number; significant: boolean;
};
type Overview = {
  debits: { total: number; by_status: Record<string, { n: number; amount_minor: number }> };
  failed_debits: number;
  recovered: { count: number; amount_minor: number };
  recovery_rate: number | null;
  agent_actions_30d: Record<string, number>;
  pending_approvals: number;
  open_cases: number;
  experiment: null | {
    id: string; name: string;
    analysis: { arms: Record<string, { n: number; recovery_rate: number; value_minor: number }>;
      incremental: Record<string, Inc> | null; note?: string };
  };
};

/* Proportion bar: thin mark, rounded data-end, value labelled in text ink (never colour-only). */
function Meter({ label, value, max, display, series = 1 }: {
  label: React.ReactNode; value: number; max: number; display: string; series?: number;
}) {
  const w = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  return (
    <div className="group">
      <div className="mb-1 flex items-center justify-between gap-3 text-sm">
        <span className="min-w-0 truncate">{label}</span>
        <span className="tabular-nums text-muted-foreground">{display}</span>
      </div>
      <div className="h-2 rounded-full bg-muted" role="img" aria-label={display}>
        <div className="h-2 rounded-full transition-all group-hover:opacity-80"
          style={{ width: `${w}%`, background: `var(--series-${series})` }} />
      </div>
    </div>
  );
}

const STATUS_ORDER = ["succeeded", "attempting", "notified", "scheduled", "failed", "cancelled"];
const rank = (s: string) => (STATUS_ORDER.includes(s) ? STATUS_ORDER.indexOf(s) : 99);

export default async function OverviewPage() {
  const o = await api<Overview>("/v1/overview");
  const succeeded = o.debits.by_status["succeeded"];
  const exp = o.experiment;
  const arms = exp ? Object.entries(exp.analysis.arms) : [];
  const actions = Object.entries(o.agent_actions_30d).sort((a, b) => b[1] - a[1]);
  const maxAction = Math.max(0, ...actions.map(([, n]) => n));
  const statuses = Object.entries(o.debits.by_status).sort((a, b) => rank(a[0]) - rank(b[0]));
  return (
    <>
      <PageHeader eyebrow="Overview" title="Revenue, protected and proven"
        subtitle="What Nirantar's agents did and what it verifiably earned — every rupee here is confirmed with your payment provider and the ledger."
        right={<Button asChild variant="outline" size="sm"><Link href="/approvals">Review approvals<ArrowRight /></Link></Button>} />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat emphasis label="Recovered (verified)" icon={<BadgeIndianRupee />} value={inr(o.recovered.amount_minor)}
          hint={`${o.recovered.count} debits · recovery rate ${pct(o.recovery_rate)}`} />
        <Stat label="Collected on schedule" icon={<Wallet />} value={inr(succeeded?.amount_minor)}
          hint={`${succeeded?.n ?? 0} of ${o.debits.total} debits`} />
        <Stat label="Failed at debit" icon={<ShieldAlert />} value={o.failed_debits} hint="provider-reported failures" />
        <Stat label="Needs a person" icon={<ClipboardCheck />} value={o.pending_approvals}
          hint={`${o.open_cases} open cases`} />
      </div>

      <div className="mt-6 grid gap-4 xl:grid-cols-3">
        <Card title="Incremental impact" className="xl:col-span-2"
          description="Treatment vs a randomised holdout — the only honest way to know what the agents added."
          action={exp && <Badge tone="info">{exp.name}</Badge>}>
          {!exp ? (
            <Empty title="No experiment running" hint="Start a holdout experiment to measure what recovery actions add." />
          ) : (
            <div className="space-y-5">
              <div className="space-y-3">
                {arms.map(([arm, a]) => (
                  <Meter key={arm} series={arm === "holdout" ? 2 : 1} value={a.recovery_rate} max={1}
                    label={<span className="flex items-center gap-2"><span className="font-medium capitalize">{arm}</span>
                      <span className="text-xs text-muted-foreground">{a.n} customers · {inr(a.value_minor)}</span></span>}
                    display={`${pct(a.recovery_rate)} recovered`} />
                ))}
              </div>
              {exp.analysis.incremental && Object.keys(exp.analysis.incremental).length > 0 ? (
                Object.entries(exp.analysis.incremental).map(([arm, inc]) => (
                  <div key={arm} className="rounded-lg border border-border bg-muted/40 p-4">
                    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                      <span className="text-2xl font-semibold tracking-tight">
                        {inc.incremental_recovery_rate >= 0 ? "+" : ""}{pct(inc.incremental_recovery_rate)}
                      </span>
                      <span className="text-sm text-muted-foreground">recovery points from <b className="font-medium text-foreground">{arm}</b></span>
                      <Badge tone={inc.significant ? "good" : "warn"}>{inc.significant ? "significant" : "not yet significant"}</Badge>
                    </div>
                    <div className="mt-1 text-sm text-muted-foreground">
                      95% CI {pct(inc.ci95[0])} to {pct(inc.ci95[1])} · ≈ {inr(inc.incremental_value_total_minor)} incremental
                    </div>
                  </div>
                ))
              ) : (
                <p className="text-sm text-muted-foreground">{exp.analysis.note ?? "Not enough data for an incrementality estimate yet."}</p>
              )}
            </div>
          )}
        </Card>

        <Card title="Agent actions" description="Last 30 days, by outcome"
          action={<Button asChild variant="ghost" size="sm"><Link href="/agents">Activity<ArrowRight /></Link></Button>}>
          {actions.length === 0 ? <Empty title="No agent actions yet" /> : (
            <div className="space-y-3">
              {actions.map(([s, n]) => (
                <Meter key={s} value={n} max={maxAction} display={String(n)}
                  label={<Badge>{s.replaceAll("_", " ")}</Badge>} />
              ))}
            </div>
          )}
        </Card>
      </div>

      <Card title="Debits by status" className="mt-6" description={`${o.debits.total} debits in total`}>
        {statuses.length === 0 ? <Empty title="No debits yet" hint="They appear once your history is imported." /> : (
          <div className="grid gap-x-8 gap-y-4 md:grid-cols-2">
            {statuses.map(([s, v]) => (
              <Link key={s} href={`/debits?status=${s}`} className="-m-1 rounded-md p-1 hover:bg-accent/60">
                <Meter value={v.n} max={o.debits.total} display={`${v.n} · ${inr(v.amount_minor)}`}
                  label={<Badge>{s}</Badge>} />
              </Link>
            ))}
          </div>
        )}
      </Card>
    </>
  );
}
