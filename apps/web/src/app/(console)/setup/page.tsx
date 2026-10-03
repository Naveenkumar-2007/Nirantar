import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, PageHeader } from "@/components/ui";
import { AutoRefresh, ImportButton, RazorpayForm } from "./forms";

type Step = {
  key: string; title: string; status: "done" | "todo" | "running" | "failed" | "blocked"; detail: string;
  started_at?: string; finished_at?: string; verified_at?: string;
  models?: { model: string; title: string; ready: boolean; gaps: Record<string, { need: number; have: number }> }[];
};
type Onboarding = { business: string; steps: Step[]; complete: boolean };

const TONE: Record<Step["status"], "good" | "warn" | "bad" | "info" | "neutral"> = {
  done: "good", running: "info", todo: "warn", failed: "bad", blocked: "neutral",
};
const LABEL: Record<Step["status"], string> = {
  done: "Done", running: "Running", todo: "To do", failed: "Failed", blocked: "Waiting",
};

export default async function SetupPage() {
  const ob = await api<Onboarding>("/v1/onboarding");
  const by = Object.fromEntries(ob.steps.map((s) => [s.key, s])) as Record<string, Step>;
  const done = ob.steps.filter((s) => s.status === "done").length;
  return (
    <>
      <PageHeader title={`Set up ${ob.business}`}
        subtitle="Each step is verified for real before it's marked done — nothing here is simulated."
        right={<div className="text-sm text-[var(--muted)]">{done} of {ob.steps.length} complete</div>} />
      {by.history?.status === "running" && <AutoRefresh />}
      <div className="mb-6 h-2 overflow-hidden rounded-full bg-[var(--chip)]">
        <div className="h-full rounded-full bg-[var(--accent)] transition-all" style={{ width: `${(done / ob.steps.length) * 100}%` }} />
      </div>
      <ol className="space-y-4">
        {ob.steps.map((s, i) => (
          <li key={s.key}>
            <Card>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex items-start gap-3">
                  <span className={`mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-semibold ${
                    s.status === "done" ? "bg-[var(--good-bg)] text-[var(--good-fg)]" : "bg-[var(--chip)] text-[var(--muted)]"}`}>
                    {s.status === "done" ? "✓" : i + 1}
                  </span>
                  <div>
                    <div className="font-medium">{s.title}</div>
                    <div className="mt-0.5 text-sm text-[var(--muted)]">{s.detail}</div>
                    {s.verified_at && <div className="mt-0.5 text-xs text-[var(--muted)]">Verified {ist(s.verified_at)}</div>}
                  </div>
                </div>
                <Badge tone={TONE[s.status]}>{LABEL[s.status]}</Badge>
              </div>
              {s.key === "payments" && s.status !== "done" && <div className="mt-4 max-w-md"><RazorpayForm /></div>}
              {s.key === "history" && (s.status === "todo" || s.status === "failed") && (
                <div className="mt-4"><ImportButton label={s.status === "failed" ? "Try the import again" : "Import my history"} /></div>
              )}
              {s.key === "history" && s.status === "running" && (
                <p className="mt-3 text-sm text-[var(--muted)]">Importing… this page updates by itself. Started {ist(s.started_at)}.</p>
              )}
              {s.key === "readiness" && s.models && s.models.length > 0 && (
                <ul className="mt-4 grid gap-2 sm:grid-cols-2">
                  {s.models.map((m) => (
                    <li key={m.model} className="rounded-lg border border-[var(--border)] p-3 text-sm">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium">{m.title}</span>
                        <Badge tone={m.ready ? "good" : "neutral"}>{m.ready ? "Ready" : "Needs more history"}</Badge>
                      </div>
                      {!m.ready && (
                        <div className="mt-1 text-xs text-[var(--muted)]">
                          {Object.entries(m.gaps).map(([k, g]) => `${k.replaceAll("_", " ")}: ${g.have}/${g.need}`).join(" · ")}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </li>
        ))}
      </ol>
    </>
  );
}
