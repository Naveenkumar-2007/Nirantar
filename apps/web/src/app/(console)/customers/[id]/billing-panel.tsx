"use client";

import { useActionState, useState, useTransition } from "react";
import { toast } from "sonner";
import { Ban, CalendarPlus, Copy, Loader2, RefreshCw, Send } from "lucide-react";
import { Badge } from "@/components/kit";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cancelSubscription, checkPayments, enrollCustomer, sendPaymentLink, type FormResult } from "@/lib/billing-actions";

type Plan = { plan_id: string; name: string; amount_minor: number; interval: string; active: boolean };
type Request = { request_id: string; debit_id: string; url: string; amount_minor: number; status: string; created_at: string; paid_at: string | null };
type Debit = { debit_id: string; scheduled_for: string; amount_minor: number; status: string };
type Sub = { subscription_id: string; status: string; collection_method: string; plan_name: string | null };

const rupees = (m: number) => `₹${(m / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;
const select = "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs";

export function EnrollForm({ customerId, plans }: { customerId: string; plans: Plan[] }) {
  const [state, action, pending] = useActionState<FormResult | null, FormData>(enrollCustomer, null);
  const active = plans.filter((p) => p.active);
  if (active.length === 0) return <p className="text-sm text-muted-foreground">Create a plan first (Plans page), then put this customer on it.</p>;
  const today = new Date().toISOString().slice(0, 10);
  return (
    <form action={action} className="flex flex-wrap items-end gap-3">
      <input type="hidden" name="customer_id" value={customerId} />
      <div className="min-w-56 flex-1 space-y-1"><Label htmlFor="ep">Plan</Label>
        <select id="ep" name="plan_id" className={select} required>
          {active.map((p) => <option key={p.plan_id} value={p.plan_id}>{p.name} · {rupees(p.amount_minor)} / {p.interval.replace("ly", "")}</option>)}
        </select></div>
      <div className="space-y-1"><Label htmlFor="es">First payment due</Label>
        <Input id="es" name="start_on" type="date" required min={today} defaultValue={today} /></div>
      <Button type="submit" disabled={pending}>{pending ? <Loader2 className="animate-spin" /> : <CalendarPlus />}Put on plan</Button>
      {state && <p className={`w-full text-sm ${state.ok ? "text-success" : "text-danger"}`}>{state.message}</p>}
    </form>
  );
}

export function PaymentLinks({ customerId, requests, debits, subs }: { customerId: string; requests: Request[]; debits: Debit[]; subs: Sub[] }) {
  const [pending, start] = useTransition();
  const [busy, setBusy] = useState<string | null>(null);
  const unpaid = debits.filter((d) => ["scheduled", "notified", "attempting", "failed"].includes(d.status));
  const run = (key: string, f: () => Promise<FormResult>) => { setBusy(key); start(async () => {
    const r = await f(); setBusy(null);
    if (r.ok) toast.success(r.message); else toast.error(r.message);
  }); };
  const copy = async (url: string) => { await navigator.clipboard.writeText(url); toast.success("Link copied"); };
  return (
    <div className="space-y-4">
      {unpaid.length > 0 && (
        <div className="space-y-2">
          {unpaid.map((d) => (
            <div key={d.debit_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3 text-sm">
              <span><b className="tabular-nums">{rupees(d.amount_minor)}</b> due {d.scheduled_for} <Badge>{d.status}</Badge></span>
              <Button size="sm" variant="outline" disabled={pending}
                onClick={() => run(d.debit_id, () => sendPaymentLink(d.debit_id, customerId))}>
                {busy === d.debit_id ? <Loader2 className="animate-spin" /> : <Send />}Send payment link now
              </Button>
            </div>
          ))}
        </div>
      )}
      {requests.length === 0 ? <p className="text-sm text-muted-foreground">No payment links yet.</p> : (
        <ul className="divide-y divide-border rounded-lg border border-border text-sm">
          {requests.map((r) => (
            <li key={r.request_id} className="flex flex-wrap items-center justify-between gap-2 p-3">
              <span className="min-w-0">
                <span className="tabular-nums">{rupees(r.amount_minor)}</span>{" "}
                <Badge tone={r.status === "paid" ? "good" : r.status === "expired" ? "bad" : "info"}>{r.status}</Badge>
                <a href={r.url} target="_blank" rel="noreferrer" className="ml-2 break-all text-xs text-primary hover:underline">{r.url}</a>
              </span>
              <Button size="icon" variant="ghost" aria-label="copy link" onClick={() => copy(r.url)}><Copy /></Button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={pending} onClick={() => run("check", () => checkPayments(`/customers/${customerId}`))}>
          {busy === "check" ? <Loader2 className="animate-spin" /> : <RefreshCw />}Check payments now
        </Button>
        {subs.filter((s) => s.status === "active" && s.collection_method !== "provider_subscription").map((s) => (
          <Button key={s.subscription_id} size="sm" variant="ghost" disabled={pending}
            onClick={() => { if (confirm(`Cancel ${s.plan_name ?? "this subscription"}? No further payments will be requested.`)) run(s.subscription_id, () => cancelSubscription(s.subscription_id, customerId)); }}>
            <Ban />Cancel {s.plan_name ?? "subscription"}
          </Button>
        ))}
      </div>
    </div>
  );
}
