import Link from "next/link";
import { FileClock, Landmark, Timer, Wallet } from "lucide-react";
import { api } from "@/lib/api";
import { day, inr } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat, Table, Td, Tr } from "@/components/kit";
import { InvoiceActions, NewInvoice } from "./forms";

type Bucket = { n: number; minor: number };
type Invoice = { invoice_id: string; number: string; customer_id: string; display_name: string | null; issued_on: string;
  due_on: string; amount_minor: number; paid_minor: number; status: string; last_step: string | null };
type Data = { items: Invoice[]; ageing: { buckets: Record<string, Bucket>; outstanding_minor: number;
  days_to_pay_90d: number | null; paid_90d: number; collected_30d_minor: number } };
type Customer = { customer_id: string; display_name: string | null };

const BUCKETS: [string, string][] = [["not_due", "Not due yet"], ["0_30", "1–30 days late"], ["31_60", "31–60 days"],
  ["61_90", "61–90 days"], ["90_plus", "90+ days"]];
const STEP: Record<string, string> = { reminder: "reminded", due: "due-date message", overdue_1: "1st follow-up",
  overdue_2: "2nd follow-up", final: "final notice", human: "with your team", manual: "sent by you" };

export const metadata = { title: "Receivables" };

export default async function ReceivablesPage() {
  const [d, cust] = await Promise.all([api<Data>("/v1/invoices"), api<{ items: Customer[] }>("/v1/customers?limit=200")]);
  const a = d.ageing;
  const maxB = Math.max(1, ...Object.values(a.buckets).map((b) => b.minor));
  const today = new Date().toISOString().slice(0, 10);
  const openCount = d.items.filter((i) => i.status === "open" || i.status === "partially_paid").length;
  return (
    <>
      <PageHeader eyebrow="Grow" title="Receivables"
        subtitle="Invoices to business customers. Nirantar reminds politely before and on the due date, follows up when late, asks you before any final notice, and hands over to your team after 30 days, stopping the moment an invoice is paid or disputed."
        right={<NewInvoice customers={cust.items} today={today} />} />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat emphasis label="Outstanding" icon={<Wallet />} value={inr(a.outstanding_minor)} hint={`${openCount} open invoices`} />
        <Stat label="Overdue" icon={<FileClock />} value={inr(a.outstanding_minor - (a.buckets.not_due?.minor ?? 0))} hint="past the due date" />
        <Stat label="Days to get paid" icon={<Timer />} value={a.days_to_pay_90d === null ? "—" : `${a.days_to_pay_90d} days`}
          hint={`average of ${a.paid_90d} invoices paid in 90 days`} />
        <Stat label="Collected (30 days)" icon={<Landmark />} value={inr(a.collected_30d_minor)} hint="verified with your provider" />
      </div>
      <Card title="Ageing" description="How late the money is. Older money is harder to collect." className="mt-6">
        <div className="space-y-3">
          {BUCKETS.map(([k, label], i) => {
            const b = a.buckets[k] ?? { n: 0, minor: 0 };
            return (
              <div key={k}>
                <div className="mb-1 flex justify-between text-sm"><span>{label}</span>
                  <span className="tabular-nums text-muted-foreground">{b.n} · {inr(b.minor)}</span></div>
                <div className="h-2 rounded-full bg-muted" role="img" aria-label={`${label}: ${inr(b.minor)}`}>
                  <div className="h-2 rounded-full" style={{ width: `${b.minor ? Math.max(2, (b.minor / maxB) * 100) : 0}%`, background: `var(--series-${i + 1})` }} />
                </div>
              </div>
            );
          })}
        </div>
      </Card>
      <Card title="Invoices" className="mt-6">
        {d.items.length === 0 ? <Empty title="No invoices yet" hint="Issue your first invoice: choose a customer, amount and due date." /> : (
          <Table head={["Invoice", "Customer", "Amount", "Due", "Status", "Ladder", ""]}>
            {d.items.map((i) => (
              <Tr key={i.invoice_id}>
                <Td className="font-medium">{i.number}</Td>
                <Td><Link href={`/customers/${i.customer_id}`} className="hover:underline">{i.display_name ?? "—"}</Link></Td>
                <Td className="tabular-nums">{inr(i.amount_minor)}
                  {i.paid_minor > 0 && <div className="text-xs text-muted-foreground">paid {inr(i.paid_minor)}</div>}</Td>
                <Td>{day(i.due_on)}</Td>
                <Td><Badge>{i.status.replace("_", " ")}</Badge></Td>
                <Td className="text-sm text-muted-foreground">{i.last_step ? STEP[i.last_step] ?? i.last_step : "scheduled"}</Td>
                <Td><InvoiceActions invoiceId={i.invoice_id} status={i.status} /></Td>
              </Tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
