import Link from "next/link";
import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader, Table, Td } from "@/components/ui";
import { RetireForm, TrainButton } from "./controls";

type M = Record<string, number>;
type Version = {
  version: string; stage: string; algorithm: string; feature_set: string; data_source: "tenant" | "synthetic";
  metrics: M; baseline: M; gates: { passed: boolean; failures: string[] }; canary_share: number;
  training: { rows: Record<string, number>; positives: Record<string, number>; brier_vs_prior: [number, number, number];
    importance?: Record<string, number> };
  trained_at: string; stage_changed_at: string;
};
type Event = { version: string; from_stage: string | null; to_stage: string; reason: string; actor: string; at: string };
type Monitoring = {
  computed_at: string; retrain: boolean; triggers: string[]; drift_note?: string; label_growth?: number;
  performance: Record<string, { n: number; brier: number; ece?: number; auc?: number; base_rate: number }>;
  drift: null | { share_drifted: number; reference_version: string; current_rows: number;
    columns: Record<string, { method: string; value: number; drifted: boolean }> };
};
type Resp = {
  versions: Version[]; events: Event[]; monitoring: Monitoring | null; served_30d: Record<string, number>;
  readiness: null | { ready: boolean; gaps: Record<string, { need: number; have: number }> }; can_manage: boolean;
};

const f = (x: number | undefined, d = 3) => (x === undefined || x === null ? "—" : x.toFixed(d));
const STAGE_TONE: Record<string, "good" | "info" | "warn" | "neutral" | "bad"> = {
  champion: "good", canary: "info", shadow: "warn", retired: "neutral", rejected: "bad",
};

export default async function ModelsPage() {
  const r = await api<Resp>("/v1/ml/models");
  const live = r.versions.filter((v) => ["champion", "canary", "shadow"].includes(v.stage));
  const champion = live.find((v) => v.stage === "champion");
  const served = Object.entries(r.served_30d);
  return (
    <>
      <PageHeader title="Your models"
        subtitle="Models are trained on your own history only, must beat a transparent baseline, then prove themselves on live verified outcomes before they decide anything."
        right={<div className="flex items-center gap-3">{r.can_manage && <TrainButton />}
          <Link href="/models/benchmarks" className="text-sm text-[var(--muted)] underline">Research benchmarks</Link></div>} />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Debit-failure risk (M1): who decides now">
          <div className="text-lg font-semibold">
            {champion ? <>Your model v{champion.version} <Badge tone="good">champion</Badge></> : <>Transparent prior <Badge tone="neutral">cold start</Badge></>}
          </div>
          <p className="mt-2 text-xs text-[var(--muted)]">
            {champion ? `${champion.algorithm}, promoted ${ist(champion.stage_changed_at)}.`
              : "Each subscription's own failure rate, shrunk towards your business-wide rate. Used until a trained model earns its place."}
          </p>
          {served.length > 0 && <p className="mt-2 text-xs text-[var(--muted)]">Last 30 days: {served.map(([k, n]) => `${n} by ${k}`).join(" · ")}</p>}
        </Card>
        <Card title="Training data">
          {!r.readiness ? <p className="text-sm text-[var(--muted)]">No data synced yet — see Data & readiness.</p>
            : r.readiness.ready ? <p className="text-sm"><Badge tone="good">enough data</Badge> to train a model for your business.</p>
            : (<ul className="space-y-1 text-sm">{Object.entries(r.readiness.gaps).map(([k, g]) =>
                <li key={k}><Mono>{k}</Mono> {g.have.toLocaleString("en-IN")} / {g.need.toLocaleString("en-IN")}</li>)}</ul>)}
        </Card>
        <Card title="Monitoring">
          {!r.monitoring ? <p className="text-sm text-[var(--muted)]">No monitoring report yet.</p> : (
            <div className="space-y-1 text-sm">
              <p>{r.monitoring.retrain ? <Badge tone="warn">retrain triggered</Badge> : <Badge tone="good">no triggers</Badge>} <span className="text-xs text-[var(--muted)]">{ist(r.monitoring.computed_at)}</span></p>
              {r.monitoring.triggers.map((t) => <p key={t} className="text-xs">• {t}</p>)}
              {r.monitoring.drift ? <p className="text-xs text-[var(--muted)]">Feature drift: {(r.monitoring.drift.share_drifted * 100).toFixed(0)}% of features vs the training window of v{r.monitoring.drift.reference_version} ({r.monitoring.drift.current_rows} served predictions)</p>
                : <p className="text-xs text-[var(--muted)]">{r.monitoring.drift_note ?? "Drift not computed yet."}</p>}
            </div>
          )}
        </Card>
      </div>

      <Card title="Model versions" className="mt-4">
        {r.versions.length === 0 ? <Empty title="No model trained yet" hint="Training starts automatically once your data passes the readiness gate." /> : (
          <Table head={["Version", "Stage", "Algorithm · data", "Test AUC / Brier", "Prior AUC / Brier", "Gate", "Live (labelled)", ""]}>
            {r.versions.map((v) => {
              const perf = r.monitoring?.performance?.[v.version];
              return (
                <tr key={v.version}>
                  <Td><Mono>{v.version.startsWith("rejected") ? "—" : `v${v.version}`}</Mono><div className="text-xs text-[var(--muted)]">{ist(v.trained_at)}</div></Td>
                  <Td><Badge tone={STAGE_TONE[v.stage]}>{v.stage}</Badge>{v.stage === "canary" && <div className="text-xs text-[var(--muted)]">{Math.round(v.canary_share * 100)}% of debits</div>}</Td>
                  <Td className="text-xs">{v.algorithm}<div>{v.data_source === "synthetic" ? <Badge tone="warn">synthetic data</Badge> : <Badge tone="neutral">your data</Badge>} {Object.values(v.training.rows).reduce((a, b) => a + b, 0).toLocaleString("en-IN")} rows</div></Td>
                  <Td className="tabular-nums">{f(v.metrics.auc)} / {f(v.metrics.brier, 4)}</Td>
                  <Td className="tabular-nums">{f(v.baseline.auc)} / {f(v.baseline.brier, 4)}</Td>
                  <Td>{v.gates.passed ? <Badge tone="good">passed</Badge> : <><Badge tone="bad">failed</Badge><div className="mt-1 max-w-xs text-xs text-[var(--muted)]">{v.gates.failures.join("; ")}</div></>}</Td>
                  <Td className="tabular-nums text-xs">{perf ? `${perf.n} · Brier ${f(perf.brier, 4)}${perf.auc !== undefined ? ` · AUC ${f(perf.auc)}` : ""}` : "—"}</Td>
                  <Td>{r.can_manage && ["champion", "canary", "shadow"].includes(v.stage) && <RetireForm version={v.version} />}</Td>
                </tr>
              );
            })}
          </Table>
        )}
        <p className="mt-2 text-xs text-[var(--muted)]">Rollout: shadow (scored, never decides) → canary (decides for a fixed share) → champion, only when live verified outcomes show it is at least as good as what decides today. A champion that degrades is rolled back to the prior automatically.</p>
      </Card>

      {r.events.length > 0 && (
        <Card title="Lifecycle events" className="mt-4">
          <ul className="space-y-1 text-sm">
            {r.events.map((e, i) => (
              <li key={i}><span className="text-xs text-[var(--muted)]">{ist(e.at)}</span> <Mono>v{e.version.startsWith("rejected") ? "—" : e.version}</Mono> {e.from_stage ?? "new"} → <Badge tone={STAGE_TONE[e.to_stage]}>{e.to_stage}</Badge> <span className="text-xs text-[var(--muted)]">{e.reason} · {e.actor}</span></li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}
