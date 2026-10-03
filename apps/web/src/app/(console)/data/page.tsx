import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader, Stat, Table, Td } from "@/components/ui";
import { SyncButton } from "./sync-button";

type Gap = { need: number; have: number };
type Report = {
  computed_at: string;
  history_days: number;
  layers: Record<"bronze" | "silver" | "gold", Record<string, number>>;
  quarantine: { rows: number; by_table: Record<string, number>; top_reasons: Record<string, number> };
  labels: {
    cycles: number; attempted_cycles: number; final_cycles: number; first_attempt_failures: number;
    recovered_cycles: number; failure_rate: number | null; recovery_rate: number | null; recovery_cohort: number;
    failures_awaiting_outcome: number; by_source: Record<string, number>; first_cycle_at: string | null; last_cycle_at: string | null;
  };
  subscriptions: { total: number; ended: number };
  readiness: Record<string, { title: string; ready: boolean; gaps: Record<string, Gap> }>;
  sources: { provider: string; mode: string; created_at: string }[];
  backfill: { provider: string; entity: string; settled_until: string; rows: number }[];
};
type Run = {
  run_id: string; trigger: string; status: string; error: string | null; started_at: string; finished_at: string | null;
  steps: { step: string; ms: number }[];
};

const LABEL: Record<string, string> = {
  attempted_cycles: "billing cycles attempted", final_cycles: "billing cycles with a final outcome", first_attempt_failures: "first-attempt failures",
  recovered_cycles: "recovered cycles", history_days: "days of history", subscriptions: "subscriptions",
  churned_subscriptions: "churned subscriptions", involuntary_churn: "payment-driven churns",
  voluntary_churn: "voluntary churns",
};

function Progress({ have, need }: Gap) {
  const pct = Math.min(100, Math.round((have / need) * 100));
  return (
    <div className="h-1.5 w-full rounded-full bg-[var(--chip)]" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <div className="h-1.5 rounded-full bg-[var(--accent)]" style={{ width: `${pct}%` }} />
    </div>
  );
}

function duration(r: Run) {
  if (!r.finished_at) return "running";
  return `${((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000).toFixed(1)}s`;
}

export default async function DataPage() {
  const [{ report }, runs] = await Promise.all([
    api<{ report: Report | null }>("/v1/data/health"),
    api<{ items: Run[] }>("/v1/data/runs?limit=10"),
  ]);
  return (
    <>
      <PageHeader title="Data & model readiness"
        subtitle="Your history flows from your payment provider and from Nirantar into a lakehouse (raw → cleaned → training-ready). Models are trained on YOUR data only once there is enough of it."
        right={<SyncButton />} />
      {!report ? (
        <Empty title="No data synced yet" hint="Press “Sync now” to import your provider history and build the training tables." />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <Stat label="Billing cycles" value={report.labels.cycles.toLocaleString("en-IN")} hint={`${report.labels.final_cycles} with a final outcome`} />
            <Stat label="First-attempt failure rate" value={report.labels.failure_rate === null ? "—" : `${(report.labels.failure_rate * 100).toFixed(1)}%`}
              hint={`${report.labels.first_attempt_failures} of ${report.labels.attempted_cycles} attempted cycles`} />
            <Stat label="Recovery rate (failures ≥30 days old)" value={report.labels.recovery_rate === null ? "—" : `${(report.labels.recovery_rate * 100).toFixed(1)}%`}
              hint={report.labels.recovery_rate === null
                ? `too few failures old enough to measure (${report.labels.recovery_cohort} of 30 needed) · ${report.labels.recovered_cycles} recovered so far`
                : `cohort of ${report.labels.recovery_cohort} · ${report.labels.failures_awaiting_outcome} newer failures still open`} />
            <Stat label="History covered" value={`${report.history_days} days`}
              hint={report.labels.first_cycle_at ? `${ist(report.labels.first_cycle_at)} → ${ist(report.labels.last_cycle_at)}` : undefined} />
            <Stat label="Quarantined rows" value={report.quarantine.rows} hint="rows that broke a data contract" />
          </div>

          <Card title="Model readiness (trained per business, on your own data)">
            <div className="grid gap-4 md:grid-cols-2">
              {Object.entries(report.readiness).map(([key, r]) => (
                <div key={key} className="rounded-lg border border-[var(--border)] p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium">{r.title}</span>
                    {r.ready ? <Badge tone="good">ready to train</Badge> : <Badge tone="warn">collecting data</Badge>}
                  </div>
                  {r.ready ? <p className="mt-2 text-xs text-[var(--muted)]">Enough evidence for a per-business model.</p> : (
                    <ul className="mt-2 space-y-2">
                      {Object.entries(r.gaps).map(([k, g]) => (
                        <li key={k} className="text-xs text-[var(--muted)]">
                          <div className="mb-1 flex justify-between"><span>{LABEL[k] ?? k}</span><span className="tabular-nums">{g.have.toLocaleString("en-IN")} / {g.need.toLocaleString("en-IN")}</span></div>
                          <Progress have={g.have} need={g.need} />
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
            <p className="mt-3 text-xs text-[var(--muted)]">Until a model is ready, decisions use rules and platform-wide starting estimates, and say so.</p>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Sources">
              {report.sources.length === 0 ? <p className="text-sm text-[var(--muted)]">No provider connected.</p> : (
                <ul className="space-y-1 text-sm">
                  {report.sources.map((s) => <li key={s.provider + s.mode}><Mono>{s.provider}</Mono> <Badge tone="neutral">{s.mode}</Badge> connected {ist(s.created_at)}</li>)}
                </ul>
              )}
              {report.backfill.length > 0 && (
                <Table head={["Entity", "History imported up to", "Rows"]}>
                  {report.backfill.map((b) => (
                    <tr key={b.provider + b.entity}><Td><Mono>{b.provider}.{b.entity}</Mono></Td><Td>{ist(b.settled_until)}</Td><Td className="tabular-nums">{b.rows}</Td></tr>
                  ))}
                </Table>
              )}
              <p className="mt-2 text-xs text-[var(--muted)]">Names, phone numbers, emails and UPI ids are replaced by keyed hashes before anything is stored.</p>
            </Card>
            <Card title="Lakehouse layers">
              <Table head={["Table", "Rows"]}>
                {(["bronze", "silver", "gold"] as const).flatMap((layer) =>
                  Object.entries(report.layers[layer]).filter(([, n]) => n > 0 || layer === "gold").map(([t, n]) => (
                    <tr key={t}><Td><Mono>{t}</Mono></Td><Td className="tabular-nums">{n.toLocaleString("en-IN")}</Td></tr>
                  )))}
              </Table>
            </Card>
          </div>

          {report.quarantine.rows > 0 && (
            <Card title="Quarantine: rows kept out of training">
              <ul className="space-y-1 text-sm">
                {Object.entries(report.quarantine.top_reasons).map(([reason, n]) => <li key={reason}><Badge tone="bad">{n}</Badge> {reason}</li>)}
              </ul>
            </Card>
          )}

          <Card title="Recent pipeline runs">
            <Table head={["Started", "Trigger", "Status", "Duration", "Steps"]}>
              {runs.items.map((r) => (
                <tr key={r.run_id}>
                  <Td>{ist(r.started_at)}</Td><Td>{r.trigger}</Td>
                  <Td><Badge>{r.status}</Badge>{r.error && <div className="mt-1 max-w-xs text-xs text-[var(--bad-fg)]">{r.error}</div>}</Td>
                  <Td className="tabular-nums">{duration(r)}</Td>
                  <Td className="text-xs text-[var(--muted)]">{r.steps.map((s) => `${s.step} ${(s.ms / 1000).toFixed(1)}s`).join(" · ")}</Td>
                </tr>
              ))}
            </Table>
            <p className="mt-2 text-xs text-[var(--muted)]">Last health report {ist(report.computed_at)}. Runs also start automatically every hour and when a business is onboarded.</p>
          </Card>
        </div>
      )}
    </>
  );
}
