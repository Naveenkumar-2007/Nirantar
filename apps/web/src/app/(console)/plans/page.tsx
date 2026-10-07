import { Layers } from "lucide-react";
import { api } from "@/lib/api";
import { day, inr } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Table, Td, Tr } from "@/components/kit";
import { PlanForm } from "./plan-form";

type Plan = { plan_id: string; name: string; description: string | null; amount_minor: number; interval: string;
  collection_method: string; active: boolean; created_at: string; active_subscriptions: number };

const METHOD: Record<string, string> = {
  payment_link: "Payment link on each due date", mandate: "UPI AutoPay / e-mandate",
  provider_subscription: "Provider subscription",
};

export const metadata = { title: "Plans" };

export default async function PlansPage() {
  const { items } = await api<{ items: Plan[] }>("/v1/plans");
  return (
    <>
      <PageHeader eyebrow="Operate" title="Plans"
        subtitle="What you sell on repeat. Nirantar runs the schedule: on every due date it sends the customer a secure payment link, checks the payment with your provider, and starts recovery if it is not paid." />
      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="New plan" description="Amounts come from the plan — messages never contain a typed amount.">
          <PlanForm />
        </Card>
        <Card title="Your plans" className="xl:col-span-2">
          {items.length === 0 ? <Empty icon={<Layers />} title="No plans yet" hint="Create your first plan, then add a customer and put them on it." /> : (
            <Table head={["Plan", "Price", "Collected by", "Active customers", "Created"]}>
              {items.map((p) => (
                <Tr key={p.plan_id}>
                  <Td><span className="font-medium">{p.name}</span>{!p.active && <> <Badge tone="neutral">archived</Badge></>}
                    {p.description && <div className="text-xs text-muted-foreground">{p.description}</div>}</Td>
                  <Td className="tabular-nums">{inr(p.amount_minor)} / {p.interval.replace("ly", "")}</Td>
                  <Td className="text-sm">{METHOD[p.collection_method] ?? p.collection_method}</Td>
                  <Td className="tabular-nums">{p.active_subscriptions}</Td>
                  <Td className="text-xs text-muted-foreground">{day(p.created_at)}</Td>
                </Tr>
              ))}
            </Table>
          )}
        </Card>
      </div>
    </>
  );
}
