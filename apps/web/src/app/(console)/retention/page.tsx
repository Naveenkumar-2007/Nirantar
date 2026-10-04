import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader, Stat, Table, Td, Tr } from "@/components/kit";
import { RetentionCurve, type CurvePoint } from "./retention-curve";
import { RefreshButton } from "./refresh-button";

type ArmStats = { n: number; recovery_rate: number; value_minor: number };
type Inc = { incremental_recovery_rate: number; ci95: [number, number]; incremental_value_per_customer_minor: number; significant: boolean };
type Winback = { experiment_id: string; stratum: string; holdout_bp: number; arms: Record<string, ArmStats>;
  incremental: Record<string, Inc> | null; note?: string };
type Model = { model_name: string; version: string; stage: string; algorithm: string; metrics: Record<string, number>;
  baseline: Record<string, number | string>; gates: { passed: boolean; failures: string[] }; trained_at: string };
type RiskItem = { entity_id: string; customer_id: string; p_churn_60d: number; p_payment_driven: number | null;
  route: "fix_payment" | "retention_offer"; monthly_value_minor: number; churn_model: string };
type Resp = {
  subscriptions: null | { total: number; churned: number; involuntary: number; voluntary: number; completed: number };
  fit: null | { fitted_at: string; sbg: { alpha: number; beta: number } | null; type_rule: { p_trouble: number; p_clean: number } | null;
    report: { subscribers: number; churned: number; sbg_note?: string; clv_total_minor?: number; clv_mean_minor?: number; active_valued?: number;
      sbg?: { mean_churn_per_period: number; fit_ok: boolean; fit_check: { max_abs_error: number | null; curve: CurvePoint[] } } } };
  at_risk: null | { computed_at: string; items: RiskItem[];
    summary: { active: number; scored: number; unscored: number; average: number; max?: number; threshold: number; decided_by?: string[]; at_risk: number } };
  models: Model[]; winback: Winback[]; cases: Record<string, number>; offers: Record<string, number>; can_manage: boolean;
};

const inr = (m: number) => `₹${Math.round(m / 100).toLocaleString("en-IN")}`;
const pct = (v: number, d = 1) => `${(v * 100).toFixed(d)}%`;
const TITLE: Record<string, string> = { m6_churn: "Churn in 60 days (M6)", m13_churn_type: "Churn reason (M13)" };

export default async function RetentionPage() {
  const r = await api<Resp>("/v1/retention/overview");
  const subs = r.subscriptions;
  const sbg = r.fit?.report.sbg;
  const latest = (name: string) => r.models.find((m) => m.model_name === name);
  return (
    <>
      <PageHeader title="Retention & win-back"
        subtitle="Who leaves and why (payment trouble vs choice), what each subscriber is worth, who is at risk now, and whether win-back offers actually bring people back — measured against a holdout."
        right={r.can_manage ? <RefreshButton /> : undefined} />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Subscribers" value={subs ? subs.total.toLocaleString("en-IN") : "—"} hint={subs ? `${subs.churned} churned · ${subs.completed} completed their plan` : "sync data first"} />
        <Stat label="Why they left" value={subs && subs.churned ? pct(subs.involuntary / subs.churned, 0) : "—"}
          hint={subs && subs.churned ? `payment trouble (${subs.involuntary}) · by choice ${subs.voluntary}` : "no churn observed yet"} />
        <Stat label="Average lifetime value" value={r.fit?.report.clv_mean_minor !== undefined ? inr(r.fit.report.clv_mean_minor) : "—"}
          hint={sbg ? `sBG: ${pct(sbg.mean_churn_per_period)} churn per period on average` : r.fit?.report.sbg_note ?? "not fitted yet"} />
        <Stat label="At risk now" value={r.at_risk ? r.at_risk.summary.at_risk : "—"}
          hint={r.at_risk ? `of ${r.at_risk.summary.scored} scored · threshold ${pct(r.at_risk.summary.threshold)}` : "not scored yet"} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Retention curve: observed vs fitted (sBG)">
          {sbg ? <>
            <RetentionCurve points={sbg.fit_check.curve} />
            <p className="mt-2 text-xs text-muted-foreground">
              {sbg.fit_ok ? <Badge tone="good">fit ok</Badge> : <Badge tone="warn">fit loose</Badge>} largest gap {sbg.fit_check.max_abs_error === null ? "—" : pct(sbg.fit_check.max_abs_error)} over periods with ≥30 subscribers.
              Lifetime value = expected future payments × amount, discounted at your retention setting.
            </p>
          </> : <Empty title="Not enough churn history" hint={r.fit?.report.sbg_note ?? "Run a data sync; the fit needs ≥30 paying subscribers and ≥5 churns."} />}
        </Card>
        <Card title="Churn models (trained on your data, must beat a transparent baseline)">
          <ul className="space-y-3">
            {["m6_churn", "m13_churn_type"].map((name) => {
              const m = latest(name);
              return (
                <li key={name} className="text-sm">
                  <div className="flex items-center justify-between gap-2"><span className="font-medium">{TITLE[name]}</span>
                    {!m ? <Badge tone="neutral">not trained yet</Badge> : <Badge tone={m.stage === "champion" ? "good" : m.stage === "rejected" ? "bad" : "info"}>{m.stage}</Badge>}</div>
                  {m && <p className="mt-1 text-xs text-muted-foreground">
                    learned AUC {m.metrics.auc?.toFixed(3)} / Brier {m.metrics.brier?.toFixed(4)} vs {String(m.baseline.algorithm)} AUC {Number(m.baseline.auc).toFixed(3)} / Brier {Number(m.baseline.brier).toFixed(4)}
                    {!m.gates.passed && <> — {m.gates.failures.join("; ")}</>}</p>}
                </li>
              );
            })}
          </ul>
          <p className="mt-3 text-xs text-muted-foreground">
            Until a model is promoted, churn risk comes from the sBG tenure baseline and the reason from the calibrated payment-trouble rule{r.fit?.type_rule ? ` (${pct(r.fit.type_rule.p_trouble, 0)} payment-driven with recent trouble vs ${pct(r.fit.type_rule.p_clean, 0)} without)` : ""}.
          </p>
        </Card>
      </div>

      <Card title="At-risk subscribers" className="mt-4">
        {!r.at_risk ? <Empty title="Not scored yet" hint="Scoring runs daily after the data and model pipeline." /> :
          r.at_risk.items.length === 0 ? (
            <Empty title="No one stands out right now"
              hint={`Average 60-day churn risk ${pct(r.at_risk.summary.average)}, highest ${pct(r.at_risk.summary.max ?? 0)}, threshold ${pct(r.at_risk.summary.threshold)} (2× average or your floor). Scores come from ${(r.at_risk.summary.decided_by ?? []).join(", ") || "—"}; a tenure-only baseline cannot single out individuals — a promoted M6 model can.`} />
          ) : (
            <Table head={["Subscription", "60-day churn risk", "Payment-driven?", "Suggested route", "Monthly value"]}>
              {r.at_risk.items.slice(0, 50).map((x) => (
                <Tr key={x.entity_id}>
                  <Td><Mono>{x.entity_id}</Mono></Td><Td className="tabular-nums">{pct(x.p_churn_60d)}</Td>
                  <Td className="tabular-nums">{x.p_payment_driven === null ? "—" : pct(x.p_payment_driven, 0)}</Td>
                  <Td>{x.route === "fix_payment" ? <Badge tone="warn">fix payment method</Badge> : <Badge tone="info">retention offer</Badge>}</Td>
                  <Td className="tabular-nums">{inr(x.monthly_value_minor)}</Td>
                </Tr>
              ))}
            </Table>
          )}
        {r.at_risk && <p className="mt-2 text-xs text-muted-foreground">Scored {ist(r.at_risk.computed_at)} · {r.at_risk.summary.unscored} not scorable (no model or baseline yet).</p>}
      </Card>

      <Card title="Win-back experiments (randomised, with holdout)" className="mt-4">
        {r.winback.length === 0 ? <Empty title="No win-back campaigns yet" hint="Churned subscribers with WhatsApp + promotional consent are selected daily, then randomised into offer arms or a holdout." /> : (
          <div className="space-y-4">
            {r.winback.map((w) => (
              <div key={w.experiment_id}>
                <div className="mb-1 text-sm font-medium">{w.stratum === "involuntary" ? "Left after payment trouble" : "Left by choice"} <span className="text-xs text-muted-foreground">holdout {w.holdout_bp / 100}% · <Mono>{w.experiment_id}</Mono></span></div>
                <Table head={["Arm", "Customers", "Came back", "Revenue", "vs holdout (95% CI)"]}>
                  {Object.entries(w.arms).map(([arm, s]) => {
                    const inc = w.incremental?.[arm];
                    return (
                      <Tr key={arm}>
                        <Td><Mono>{arm}</Mono></Td><Td className="tabular-nums">{s.n}</Td><Td className="tabular-nums">{pct(s.recovery_rate)}</Td>
                        <Td className="tabular-nums">{inr(s.value_minor)}</Td>
                        <Td wrap className="text-xs">{arm === "holdout" ? "—" : !inc ? (w.note ?? "not enough data") :
                          <>{inc.incremental_recovery_rate >= 0 ? "+" : ""}{pct(inc.incremental_recovery_rate)} ({pct(inc.ci95[0])} … {pct(inc.ci95[1])}) {inc.significant ? <Badge tone="good">significant</Badge> : <Badge tone="neutral">not significant</Badge>}</>}</Td>
                      </Tr>
                    );
                  })}
                </Table>
              </div>
            ))}
          </div>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          Cases: {Object.entries(r.cases).map(([k, v]) => `${v} ${k}`).join(" · ") || "none"} · Offers: {Object.entries(r.offers).map(([k, v]) => `${v} ${k}`).join(" · ") || "none"}.
          Win-back messages are promotional: sent only with promotional consent, inside contact windows, with an opt-out; discounts above your policy threshold need a second person&apos;s approval.
        </p>
      </Card>
    </>
  );
}
