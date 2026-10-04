import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiError, api } from "@/lib/api";
import { day, inr, ist, pct } from "@/lib/format";
import { Badge, Card, Mono, PageHeader, Stat } from "@/components/kit";

type Detail = {
  debit: { debit_id: string; display_name: string | null; amount_minor: number; scheduled_for: string; status: string;
    preferred_language: string; segment: string; attempt_count: number };
  case: { case_id: string; status: string; summary: Record<string, unknown> } | null;
  predictions: { model_name: string; model_version: string; score: number }[];
  labels: { label_name: string; value: { value: boolean } }[];
  timeline: { at: string; kind: "event" | "action"; title: string; detail: Record<string, unknown> }[];
};

export default async function DebitDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let d: Detail;
  try {
    d = await api<Detail>(`/v1/debits/${encodeURIComponent(id)}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }
  const pred = d.predictions[0];
  return (
    <>
      <Link href="/debits" className="text-sm text-primary">← Debits</Link>
      <PageHeader title={`${d.debit.display_name ?? "Customer"} · ${inr(d.debit.amount_minor)}`}
        subtitle={`Due ${day(d.debit.scheduled_for)} · ${d.debit.segment} · language ${d.debit.preferred_language}`}
        right={<Badge>{d.debit.status}</Badge>} />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="M1 failure risk" value={pred ? pct(pred.score) : "—"} hint={pred ? `model v${pred.model_version}` : "no prediction"} />
        <Stat label="Attempts" value={d.debit.attempt_count} />
        <Stat label="Case" value={d.case ? <Badge>{d.case.status}</Badge> : "—"} hint={d.case?.case_id} />
        <Stat label="Labels written" value={d.labels.length}
          hint={d.labels.map((l) => `${l.label_name}=${String(l.value.value)}`).join(" · ") || "awaiting verified outcome"} />
      </div>
      <Card title="Timeline (events and agent actions)" className="mt-6">
        <ol className="relative space-y-4 border-l border-border pl-5">
          {d.timeline.map((t, i) => (
            <li key={i}>
              <span className={`absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full ${t.kind === "action" ? "bg-primary" : "bg-muted-foreground"}`} />
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-xs text-muted-foreground tabular-nums">{ist(t.at)}</span>
                <span className="font-medium">{t.title}</span>
                {t.kind === "action" && typeof t.detail.status === "string" && <Badge>{t.detail.status}</Badge>}
                {t.kind === "action" && typeof t.detail.policy === "string" && <Badge>{t.detail.policy}</Badge>}
              </div>
              <details className="mt-1">
                <summary className="cursor-pointer text-xs text-muted-foreground">details</summary>
                <pre className="mt-1 max-h-64 overflow-auto rounded bg-muted p-2 text-xs">{JSON.stringify(t.detail, null, 2)}</pre>
              </details>
            </li>
          ))}
        </ol>
        <p className="mt-4 text-xs text-muted-foreground">Debit id <Mono>{d.debit.debit_id}</Mono></p>
      </Card>
    </>
  );
}
