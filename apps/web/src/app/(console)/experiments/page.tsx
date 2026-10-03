import { api } from "@/lib/api";
import { inr, pct } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Table, Td } from "@/components/ui";

type Analysis = {
  experiment_id: string;
  arms: Record<string, { n: number; recovery_rate: number; value_minor: number }>;
  incremental: Record<string, { incremental_recovery_rate: number; ci95: [number, number];
    incremental_value_per_customer_minor: number; value_ci95_minor: [number, number];
    incremental_value_total_minor: number; significant: boolean }> | null;
  note?: string;
};

export default async function ExperimentsPage() {
  const o = await api<{ experiment: { id: string; name: string } | null }>("/v1/overview");
  if (!o.experiment) {
    return (<><PageHeader title="Experiments" /><Empty title="No experiment" hint="Create a holdout experiment to measure incremental ₹." /></>);
  }
  const a = await api<Analysis>(`/v1/experiments/${o.experiment.id}`);
  return (
    <>
      <PageHeader title="Experiments" subtitle={`${o.experiment.name} · randomized holdout; only verified outcomes count; mandatory notices are never withheld`} />
      <Card title="Arms">
        <Table head={["Arm", "Customers", "Paid or recovered", "Verified ₹"]}>
          {Object.entries(a.arms).map(([arm, s]) => (
            <tr key={arm}><Td><Badge tone={arm === "holdout" ? "neutral" : "info"}>{arm}</Badge></Td>
              <Td className="tabular-nums">{s.n}</Td><Td className="tabular-nums">{pct(s.recovery_rate)}</Td>
              <Td className="tabular-nums">{inr(s.value_minor)}</Td></tr>
          ))}
        </Table>
      </Card>
      <Card title="Incrementality" className="mt-6">
        {!a.incremental || Object.keys(a.incremental).length === 0 ? (
          <p className="text-sm text-[var(--muted)]">{a.note ?? "Not enough data yet."} The holdout needs at least 30 customers per arm before we report an estimate.</p>
        ) : (
          <Table head={["Arm", "Δ recovery (points)", "95% CI", "Δ ₹ / customer", "Δ ₹ total", ""]}>
            {Object.entries(a.incremental).map(([arm, i]) => (
              <tr key={arm}><Td>{arm}</Td><Td className="tabular-nums">{pct(i.incremental_recovery_rate)}</Td>
                <Td className="tabular-nums text-xs">{pct(i.ci95[0])} … {pct(i.ci95[1])}</Td>
                <Td className="tabular-nums">{inr(i.incremental_value_per_customer_minor)}</Td>
                <Td className="tabular-nums">{inr(i.incremental_value_total_minor)}</Td>
                <Td><Badge tone={i.significant ? "good" : "warn"}>{i.significant ? "significant" : "inconclusive"}</Badge></Td></tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
