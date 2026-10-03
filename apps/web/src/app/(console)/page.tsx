import Link from "next/link";
import { api } from "@/lib/api";
import { inr, pct } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat, Table, Td } from "@/components/ui";

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

export default async function OverviewPage() {
  const o = await api<Overview>("/v1/overview");
  const succeeded = o.debits.by_status["succeeded"];
  const exp = o.experiment;
  return (
    <>
      <PageHeader title="Overview" subtitle="What the agents did, and what it verifiably earned" />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Debits" value={o.debits.total} hint={`${succeeded?.n ?? 0} collected · ${inr(succeeded?.amount_minor)}`} />
        <Stat label="Failed at debit" value={o.failed_debits} hint="provider-reported failures" />
        <Stat label="Recovered (verified)" value={inr(o.recovered.amount_minor)} hint={`${o.recovered.count} debits · rate ${pct(o.recovery_rate)}`} />
        <Stat label="Needs a human" value={o.pending_approvals} hint={`${o.open_cases} open cases`} />
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        <Card title="Incremental impact vs randomized holdout" className="lg:col-span-2">
          {!exp ? (
            <Empty title="No experiment running" hint="Create a holdout experiment to measure incremental recovery." />
          ) : (
            <>
              <div className="mb-3 text-sm text-[var(--muted)]">
                Experiment <span className="font-medium text-[var(--fg)]">{exp.name}</span> · only verifier-confirmed outcomes count
              </div>
              <Table head={["Arm", "Customers", "Recovered or paid", "Verified ₹"]}>
                {Object.entries(exp.analysis.arms).map(([arm, a]) => (
                  <tr key={arm}>
                    <Td><Badge tone={arm === "holdout" ? "neutral" : "info"}>{arm}</Badge></Td>
                    <Td className="tabular-nums">{a.n}</Td>
                    <Td className="tabular-nums">{pct(a.recovery_rate)}</Td>
                    <Td className="tabular-nums">{inr(a.value_minor)}</Td>
                  </tr>
                ))}
              </Table>
              {exp.analysis.incremental && Object.keys(exp.analysis.incremental).length > 0 ? (
                Object.entries(exp.analysis.incremental).map(([arm, inc]) => (
                  <p key={arm} className="mt-3 text-sm">
                    <span className="font-medium">{arm}</span>: {pct(inc.incremental_recovery_rate)} points
                    (95% CI {pct(inc.ci95[0])} to {pct(inc.ci95[1])}),
                    ≈ {inr(inc.incremental_value_total_minor)} incremental.{" "}
                    <Badge tone={inc.significant ? "good" : "warn"}>{inc.significant ? "significant" : "not yet significant"}</Badge>
                  </p>
                ))
              ) : (
                <p className="mt-3 text-sm text-[var(--muted)]">{exp.analysis.note ?? "Not enough data for an incrementality estimate yet."}</p>
              )}
            </>
          )}
        </Card>
        <Card title="Agent actions, last 30 days">
          {Object.keys(o.agent_actions_30d).length === 0 ? (
            <Empty title="No agent actions yet" />
          ) : (
            <ul className="space-y-2">
              {Object.entries(o.agent_actions_30d).map(([s, n]) => (
                <li key={s} className="flex items-center justify-between text-sm">
                  <Badge>{s}</Badge><span className="tabular-nums">{n}</span>
                </li>
              ))}
            </ul>
          )}
          <Link href="/agents" className="mt-4 inline-block text-sm text-[var(--accent)]">See agent activity →</Link>
        </Card>
      </div>

      <Card title="Debits by status" className="mt-6">
        <Table head={["Status", "Count", "Amount"]}>
          {Object.entries(o.debits.by_status).map(([s, v]) => (
            <tr key={s}>
              <Td><Link href={`/debits?status=${s}`}><Badge>{s}</Badge></Link></Td>
              <Td className="tabular-nums">{v.n}</Td>
              <Td className="tabular-nums">{inr(v.amount_minor)}</Td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
