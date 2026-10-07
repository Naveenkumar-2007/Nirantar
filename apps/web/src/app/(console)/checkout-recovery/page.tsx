import Link from "next/link";
import { BadgeCheck, MousePointerClick, ShoppingCart, Sigma } from "lucide-react";
import { api } from "@/lib/api";
import { inr, ist } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat, Table, Td, Tr } from "@/components/kit";

type Arm = { n: number; recovery_rate: number; value_minor: number };
type Inc = { incremental_recovery_rate: number; ci95: [number, number]; incremental_value_total_minor: number;
  significant: boolean };
type Stats = {
  window_days: number;
  funnel: { checkouts: number; paid_unaided: number; reached_payment: number; had_failure: number; dropped_off: number;
    contacted: number; recovered: number; verified_minor: number; open_minor: number };
  causes: Record<string, number>;
  experiment: { arms: Record<string, Arm>; incremental: Record<string, Inc> | null; note?: string };
};
type Session = { session_id: string; checkout_ref: string; customer_id: string | null; display_name: string | null;
  amount_minor: number; stage: string; status: string; attempts: number; cause: string | null; arm: string | null;
  paid_via: string | null; first_contact_at: string | null; created_at: string; last_step: string | null };

const CAUSE: Record<string, string> = {
  abandoned_cart: "Left before paying", abandoned_at_payment: "Left on the payment page", bank_issue: "Bank had a problem",
  insufficient_funds: "Not enough balance", card_problem: "Card problem", limit_exceeded: "Over the limit",
  customer_cancelled: "Cancelled the payment", payment_failed: "Payment failed", repeated_failures: "Failed 3+ times",
};
const STEP: Record<string, string> = { nudge: "reminded", follow_up: "followed up", escalate: "with your team",
  held_out: "holdout (not contacted)", ineligible: "not eligible" };
const pct = (x: number) => `${(x * 100).toFixed(1)}%`;

export const metadata = { title: "Checkout recovery" };

export default async function CheckoutRecoveryPage() {
  const [s, list] = await Promise.all([api<Stats>("/v1/checkout-recovery"), api<{ items: Session[] }>("/v1/checkout-sessions")]);
  const f = s.funnel;
  const inc = s.experiment.incremental?.treatment;
  const steps: [string, number][] = [["Checkouts started", f.checkouts], ["Reached payment", f.reached_payment],
    ["Dropped off", f.dropped_off], ["Reminded", f.contacted], ["Recovered", f.recovered]];
  const top = Math.max(1, f.checkouts);
  const causes = Object.entries(s.causes);
  return (
    <>
      <PageHeader eyebrow="Grow" title="Checkout recovery"
        subtitle="Customers who left at checkout or whose payment failed get one reminder (and at most one follow-up) worded for what went wrong, with a secure link for exactly their cart. A holdout group is never contacted, so the recovery shown here is what Nirantar added, not what would have happened anyway." />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat emphasis label="Recovered (verified)" icon={<BadgeCheck />} value={inr(f.verified_minor)}
          hint={`${f.recovered} checkouts · confirmed by your provider`} />
        <Stat label="Recovery rate" icon={<MousePointerClick />} value={f.contacted ? pct(f.recovered / f.contacted) : "—"}
          hint={`of ${f.contacted} reminded`} />
        <Stat label="Still open" icon={<ShoppingCart />} value={inr(f.open_minor)} hint="checkouts in progress" />
        <Stat label="Uplift vs holdout" icon={<Sigma />}
          value={inc ? `${inc.incremental_recovery_rate >= 0 ? "+" : ""}${pct(inc.incremental_recovery_rate)}` : "—"}
          hint={inc ? `95% CI ${pct(inc.ci95[0])} to ${pct(inc.ci95[1])}${inc.significant ? " · significant" : ""}`
            : s.experiment.note ?? "collecting holdout data"} />
      </div>
      <div className="mt-6 grid grid-cols-1 gap-6 xl:grid-cols-2">
        <Card title="Funnel" description={`Last ${s.window_days} days`}>
          <div className="space-y-3">
            {steps.map(([label, n], i) => (
              <div key={label}>
                <div className="mb-1 flex justify-between text-sm"><span>{label}</span>
                  <span className="tabular-nums text-muted-foreground">{n}</span></div>
                <div className="h-2 rounded-full bg-muted" role="img" aria-label={`${label}: ${n}`}>
                  <div className="h-2 rounded-full" style={{ width: `${n ? Math.max(2, (n / top) * 100) : 0}%`, background: `var(--series-${i + 1})` }} />
                </div>
              </div>
            ))}
          </div>
        </Card>
        <Card title="Why checkouts did not complete" description="Diagnosed from what the checkout and the provider reported">
          {causes.length === 0 ? <Empty title="Nothing diagnosed yet" hint="Causes appear once a checkout goes quiet for 30 minutes." /> : (
            <ul className="divide-y divide-border">
              {causes.map(([c, n]) => (
                <li key={c} className="flex items-center justify-between py-2 text-sm">
                  <span>{CAUSE[c] ?? c}</span><span className="tabular-nums text-muted-foreground">{n}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      <Card title="Checkouts" className="mt-6"
        description="Your store reports checkout events to POST /v1/checkout/events with a store key that can do nothing else.">
        {list.items.length === 0 ? <Empty title="No checkouts yet" hint="Connect your store: send initiated, payment_page, payment_failed and paid events." /> : (
          <Table head={["Order", "Customer", "Amount", "Why", "Status", "Recovery", "Started"]}>
            {list.items.map((k) => (
              <Tr key={k.session_id}>
                <Td className="font-medium">{k.checkout_ref}</Td>
                <Td>{k.customer_id ? <Link href={`/customers/${k.customer_id}`} className="hover:underline">{k.display_name ?? "—"}</Link> : "—"}</Td>
                <Td className="tabular-nums">{inr(k.amount_minor)}</Td>
                <Td className="text-sm">{k.cause ? CAUSE[k.cause] ?? k.cause : k.status === "open" ? "in progress" : "—"}</Td>
                <Td><Badge>{k.status === "paid" && k.paid_via === "recovery_link" ? "recovered" : k.status}</Badge></Td>
                <Td className="text-sm text-muted-foreground">{k.last_step ? STEP[k.last_step] ?? k.last_step : "—"}</Td>
                <Td className="text-sm text-muted-foreground">{ist(k.created_at)}</Td>
              </Tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
