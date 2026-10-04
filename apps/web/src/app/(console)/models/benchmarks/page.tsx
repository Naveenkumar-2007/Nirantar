import { api } from "@/lib/api";
import { pct } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Table, Td, Tr } from "@/components/kit";

type Metrics = Record<string, number>;
type Models = {
  ml: null | {
    data_source: string;
    m1: { gate_passed: boolean; champion_version: string | null; baseline: Metrics; advanced: Metrics };
    m4: { champion: string | null; ewma: Metrics; bocpd: Metrics };
    m5: { mode: string; reasons: string[] };
    m10: { gate_passed: boolean; baseline: Metrics; advanced: Metrics };
  };
  rag: null | { variants: Record<string, { recall_at_k: number; mrr: number }>; k: number; n_questions: number };
  voice: null | { languages: Record<string, { mean_cer: number; intent_agreement: number }>; turn: { total_ms: number; target_ms: number } };
};

const n = (x: number | undefined, d = 3) => (x === undefined ? "—" : x.toFixed(d));

export default async function BenchmarksPage() {
  const m = await api<Models>("/v1/models");
  return (
    <>
      <PageHeader title="Research benchmarks (synthetic)" subtitle="Platform-level method benchmarks on the RecurSim simulator. Not your data, not production performance, never used for your decisions." />
      {!m.ml ? <Empty title="No evaluation results" hint="Run: uv run python -m nirantar.ml.pipelines" /> : (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="M1 debit-failure predictor">
            <p className="mb-2 text-sm"><Badge tone={m.ml.m1.gate_passed ? "good" : "bad"}>{m.ml.m1.gate_passed ? "gate passed" : "gate failed"}</Badge> champion v{m.ml.m1.champion_version ?? "—"} · source {m.ml.data_source}</p>
            <Table head={["Metric", "Logistic baseline", "LightGBM + isotonic"]}>
              {["auc", "pr_auc", "brier", "ece"].map((k) => (
                <Tr key={k}><Td>{k}</Td><Td className="tabular-nums">{n(m.ml!.m1.baseline[k])}</Td><Td className="tabular-nums">{n(m.ml!.m1.advanced[k])}</Td></Tr>
              ))}
            </Table>
          </Card>
          <Card title="M4 bank health">
            <p className="mb-2 text-sm">Champion: <Badge tone="good">{m.ml.m4.champion ?? "none"}</Badge></p>
            <Table head={["Detector", "Recall", "False alarms / bank-week"]}>
              <Tr><Td>EWMA z-score</Td><Td>{pct(m.ml.m4.ewma.incident_recall)}</Td><Td>{n(m.ml.m4.ewma.false_alarms_per_bank_week, 2)}</Td></Tr>
              <Tr><Td>BOCPD</Td><Td>{pct(m.ml.m4.bocpd.incident_recall)}</Td><Td>{n(m.ml.m4.bocpd.false_alarms_per_bank_week, 2)}</Td></Tr>
            </Table>
          </Card>
          <Card title="M5 uplift">
            <p className="text-sm"><Badge tone={m.ml.m5.mode === "promoted" ? "good" : "warn"}>{m.ml.m5.mode}</Badge></p>
            <ul className="mt-2 list-disc pl-5 text-xs text-muted-foreground">{m.ml.m5.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          </Card>
          <Card title="M10 cash forecast">
            <p className="text-sm"><Badge tone={m.ml.m10.gate_passed ? "good" : "bad"}>{m.ml.m10.gate_passed ? "gate passed" : "gate failed"}</Badge></p>
            <p className="mt-2 text-sm">WAPE {pct(m.ml.m10.advanced.wape)} (naive {pct(m.ml.m10.baseline.wape)}) · 90% interval coverage {pct(m.ml.m10.advanced.interval_coverage)}</p>
          </Card>
        </div>
      )}
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="RAG retrieval">
          {!m.rag ? <Empty title="Not run" hint="uv run python -m nirantar.rag.evaluate" /> : (
            <Table head={["Variant", `Recall@${m.rag.k}`, "MRR"]}>
              {Object.entries(m.rag.variants).map(([v, r]) => (<Tr key={v}><Td>{v}</Td><Td>{pct(r.recall_at_k)}</Td><Td>{n(r.mrr)}</Td></Tr>))}
            </Table>
          )}
        </Card>
        <Card title="Voice (Sarvam, live)">
          {!m.voice ? <Empty title="Not run" hint="uv run python -m nirantar.voice.benchmark" /> : (
            <>
              <Table head={["Language", "Intent agreement", "Raw CER"]}>
                {Object.entries(m.voice.languages).map(([l, v]) => (<Tr key={l}><Td>{l}</Td><Td>{pct(v.intent_agreement, 0)}</Td><Td>{n(v.mean_cer)}</Td></Tr>))}
              </Table>
              <p className="mt-2 text-sm">Turn latency {m.voice.turn.total_ms} ms (target {m.voice.turn.target_ms} ms)</p>
            </>
          )}
        </Card>
      </div>
    </>
  );
}
