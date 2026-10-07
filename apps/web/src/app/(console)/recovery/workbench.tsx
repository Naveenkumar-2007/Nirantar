"use client";

import { useMemo, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { CircleCheck, CircleSlash, Eye, Loader2, Rocket, ShieldCheck, TriangleAlert } from "lucide-react";
import { Badge, Card, Table, Td, Tr } from "@/components/kit";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import { launchBatch, planBatch, type Plan } from "./actions";

export type QueueItem = {
  item_id: string; kind: "failed_debit" | "at_risk_debit" | "mandate"; subject_id: string; customer_id: string;
  display_name: string | null; language: string | null; amount_minor: number; due: string | null;
  reason: string | null; attempts?: number; contacts?: number; last_contact_at?: string | null;
  probability: number | null; probability_source: string | null; expected_minor: number | null;
  state: string; why: string; case_id?: string | null; action: string | null;
};

const KIND: Record<string, string> = { failed_debit: "Declined", at_risk_debit: "At risk", mandate: "Mandate" };
const ACTION: Record<string, string> = {
  payment_link_whatsapp: "Payment link on WhatsApp", predebit_notice: "Pre-debit notice now",
  mandate_repair: "Mandate repair request",
};
const STATE: Record<string, { label: string; tone: "good" | "bad" | "warn" | "info" | "neutral" }> = {
  stalled: { label: "stalled", tone: "warn" }, promise_broken: { label: "promise broken", tone: "bad" },
  promised: { label: "promised", tone: "info" }, blocked: { label: "blocked by policy", tone: "bad" },
  needs_approval: { label: "needs approval", tone: "warn" }, agent_working: { label: "agent working", tone: "info" },
  in_batch: { label: "in a batch", tone: "neutral" }, at_risk: { label: "likely to fail", tone: "warn" },
  mandate_problem: { label: "mandate problem", tone: "bad" },
};
const rupees = (minor: number) =>
  `₹${(minor / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

export function Workbench({ items }: { items: QueueItem[] }) {
  const router = useRouter();
  const [tab, setTab] = useState("all");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [plan, setPlan] = useState<Plan | null>(null);
  const [name, setName] = useState(() => `Recovery ${new Date().toLocaleDateString("en-IN", { day: "numeric", month: "short" })}`);
  const [holdout, setHoldout] = useState(20);
  const [windowDays, setWindowDays] = useState(7);
  const [previewing, startPreview] = useTransition();
  const [launching, startLaunch] = useTransition();

  const shown = useMemo(() => items.filter((x) => tab === "all" || x.kind === tab), [items, tab]);
  const actionable = shown.filter((x) => x.action);
  const sel = items.filter((x) => picked.has(x.item_id));
  const selAmount = sel.reduce((s, x) => s + x.amount_minor, 0);

  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const pickWhere = (f: (x: QueueItem) => boolean) => setPicked(new Set(actionable.filter(f).map((x) => x.item_id)));

  const preview = () => startPreview(async () => {
    const r = await planBatch([...picked]);
    if (r.ok) setPlan(r.data); else toast.error(r.message);
  });
  const launch = () => startLaunch(async () => {
    const r = await launchBatch({ itemIds: [...picked], name, holdoutPct: holdout, windowDays });
    if (!r.ok) { toast.error(r.message); return; }
    toast.success("Batch launched — the workflow is sending now");
    router.push(`/recovery/batches/${r.data.batch_id}`);
  });

  const counts = { all: items.length, failed_debit: 0, at_risk_debit: 0, mandate: 0 } as Record<string, number>;
  items.forEach((x) => { counts[x.kind] += 1; });

  return (
    <Card title="Recovery queue" description="Sorted by expected recoverable rupees. Select items and preview — nothing is sent until you launch."
      action={
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => pickWhere((x) => ["stalled", "promise_broken"].includes(x.state))}>Select stalled</Button>
          <Button variant="ghost" size="sm" onClick={() => setPicked(new Set())} disabled={picked.size === 0}>Clear</Button>
        </div>}>
      <Tabs value={tab} onValueChange={setTab} className="mb-3">
        <TabsList>
          {(["all", "failed_debit", "at_risk_debit", "mandate"] as const).map((k) => (
            <TabsTrigger key={k} value={k}>{k === "all" ? "All" : KIND[k]} <span className="ml-1 tabular-nums text-muted-foreground">{counts[k]}</span></TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <Table head={["", "Customer", "Amount", "Expected", "State", "Why", "Next action"]}>
        {shown.map((x) => (
          <Tr key={x.item_id} data-state={picked.has(x.item_id) ? "selected" : undefined}>
            <Td>
              <input type="checkbox" aria-label={`select ${x.display_name ?? x.item_id}`} disabled={!x.action}
                checked={picked.has(x.item_id)} onChange={() => toggle(x.item_id)} className="size-4 accent-[var(--primary)]" />
            </Td>
            <Td>
              <Link href={`/customers/${x.customer_id}`} className="font-medium hover:underline">{x.display_name ?? "Customer"}</Link>
              <div className="text-xs text-muted-foreground">{KIND[x.kind]}{x.due ? ` · due ${x.due}` : ""}{x.reason ? ` · ${x.reason}` : ""}</div>
            </Td>
            <Td className="tabular-nums">{rupees(x.amount_minor)}</Td>
            <Td className="tabular-nums">
              {x.expected_minor === null ? <span className="text-muted-foreground">—</span> : rupees(x.expected_minor)}
              {x.probability !== null && <div className="text-xs text-muted-foreground" title={x.probability_source ?? ""}>
                {Math.round(x.probability * 100)}% · {x.probability_source}</div>}
            </Td>
            <Td><Badge tone={STATE[x.state]?.tone ?? "neutral"}>{STATE[x.state]?.label ?? x.state}</Badge></Td>
            <Td wrap className="text-sm text-muted-foreground">{x.why}</Td>
            <Td className="text-sm">{x.action ? ACTION[x.action] : <span className="text-muted-foreground">watch</span>}</Td>
          </Tr>
        ))}
      </Table>

      <div className={cn("sticky bottom-3 z-10 mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card/95 px-4 py-3 shadow-card backdrop-blur",
        picked.size === 0 && "hidden")}>
        <div className="text-sm"><b className="tabular-nums">{picked.size}</b> selected · <span className="tabular-nums">{rupees(selAmount)}</span> at stake</div>
        <Button onClick={preview} disabled={previewing}>
          {previewing ? <Loader2 className="animate-spin" /> : <Eye />}Preview batch
        </Button>
      </div>

      <Dialog open={plan !== null} onOpenChange={(o) => { if (!o) setPlan(null); }}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Preview: {plan?.items.length} customers</DialogTitle>
            <DialogDescription>Exactly what each customer would receive, and what the Compliance Guardian decides right now. Policy is checked again at send time.</DialogDescription>
          </DialogHeader>
          {plan && (
            <>
              <div className="grid grid-cols-3 gap-3 text-sm">
                <Summary icon={<CircleCheck className="text-success" />} label="will be sent" n={plan.counts.ALLOW ?? 0} />
                <Summary icon={<ShieldCheck className="text-warning" />} label="need a person's approval" n={plan.counts.REQUIRE_APPROVAL ?? 0} />
                <Summary icon={<CircleSlash className="text-danger" />} label="refused by policy" n={(plan.counts.DENY ?? 0) + (plan.counts.other ?? 0)} />
              </div>
              <ul className="divide-y divide-border rounded-lg border border-border">
                {plan.items.map((p) => (
                  <li key={p.item_id} className="p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                      <span className="font-medium">{p.display_name ?? "Customer"} · {rupees(p.amount_minor)}</span>
                      <Badge tone={p.decision === "ALLOW" ? "good" : p.decision === "DENY" ? "bad" : "warn"}>{p.decision.toLowerCase().replaceAll("_", " ")}</Badge>
                    </div>
                    {p.message && <p className="mt-2 whitespace-pre-wrap rounded-md bg-muted/60 p-2 text-sm">{p.message.replace("{link}", "‹secure payment link›")}</p>}
                    {!p.message && <p className="mt-1 text-xs text-muted-foreground">{ACTION[p.action]} · approved template</p>}
                    {p.checks.flatMap((c) => c.messages).map((m, i) => (
                      <p key={i} className="mt-1 flex items-start gap-1 text-xs text-muted-foreground"><TriangleAlert className="mt-0.5 size-3 shrink-0" />{m}</p>
                    ))}
                  </li>
                ))}
              </ul>
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="space-y-1 sm:col-span-3"><Label htmlFor="bn">Batch name</Label>
                  <Input id="bn" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} /></div>
                <div className="space-y-1"><Label htmlFor="ho">Holdout (%)</Label>
                  <Input id="ho" type="number" min={0} max={50} value={holdout} onChange={(e) => setHoldout(Number(e.target.value))} />
                  <p className="text-xs text-muted-foreground">Randomly left alone, to prove what the batch added.</p></div>
                <div className="space-y-1"><Label htmlFor="wd">Measure for (days)</Label>
                  <Input id="wd" type="number" min={1} max={30} value={windowDays} onChange={(e) => setWindowDays(Number(e.target.value))} /></div>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setPlan(null)}>Cancel</Button>
                <Button onClick={launch} disabled={launching || (plan.counts.ALLOW ?? 0) + (plan.counts.REQUIRE_APPROVAL ?? 0) === 0}>
                  {launching ? <Loader2 className="animate-spin" /> : <Rocket />}Launch batch
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Summary({ icon, label, n }: { icon: React.ReactNode; label: string; n: number }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-border p-3 [&>svg]:size-5">
      {icon}<div><div className="text-lg font-semibold tabular-nums">{n}</div><div className="text-xs text-muted-foreground">{label}</div></div>
    </div>
  );
}
