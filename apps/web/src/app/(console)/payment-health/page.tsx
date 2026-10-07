import { Activity, BadgeIndianRupee, ShieldCheck, Siren } from "lucide-react";
import { api } from "@/lib/api";
import { inr, ist, pct } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat, Table, Td, Tr } from "@/components/kit";

type Incident = {
  incident_id: string; rail: string; issuer: string; started_at: string; detected_at: string; ended_at: string | null;
  status: "open" | "closed"; baseline_rate: number; peak_rate: number; attempts: number; failures: number;
  detector: string; recovery_done: boolean;
  yours: { affected: number; contacted: number; recovered: number; recovered_minor: number } | null;
};

const RAIL: Record<string, string> = { upi: "UPI", netbanking: "Net banking", card: "Card", emandate: "e-mandate", wallet: "Wallet", nach: "NACH" };

function duration(from: string, to: string | null): string {
  const mins = Math.round(((to ? new Date(to) : new Date()).getTime() - new Date(from).getTime()) / 60000);
  return mins < 90 ? `${mins} min` : `${Math.round(mins / 60)} h`;
}

export const metadata = { title: "Payment health" };

export default async function PaymentHealthPage() {
  const { incidents, open } = await api<{ incidents: Incident[]; open: number }>("/v1/payment-health");
  const mine = incidents.filter((i) => i.yours);
  const affected = mine.reduce((s, i) => s + (i.yours?.affected ?? 0), 0);
  const recovered = mine.reduce((s, i) => s + (i.yours?.recovered_minor ?? 0), 0);
  return (
    <>
      <PageHeader eyebrow="Intelligence" title="Payment health"
        subtitle="When a bank or UPI app is failing, Nirantar notices from payment patterns across the platform, stops treating your customers as defaulters for the bank's fault, and — once it recovers — sends each affected customer an honest retry link." />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat emphasis={open > 0} label="Live incidents" icon={<Siren />} value={open} hint={open ? "customers on these issuers are not chased" : "all issuers healthy"} />
        <Stat label="Incidents (30 days)" icon={<Activity />} value={incidents.length} hint="detected by change-point model M4" />
        <Stat label="Your payments hit" icon={<ShieldCheck />} value={affected} hint="failed because of a bank, not your customer" />
        <Stat label="Recovered after incidents" icon={<BadgeIndianRupee />} value={inr(recovered)} hint="verified with your provider" />
      </div>
      <Card title="Incidents" description="Failure rate per issuer compared with its normal rate. Only aggregates are shared across businesses — never customer data." className="mt-6">
        {incidents.length === 0 ? <Empty icon={<ShieldCheck />} title="No incidents in the last 30 days" hint="Every issuer your customers pay through has been healthy." /> : (
          <Table head={["Issuer", "Status", "Failure rate", "Started", "Duration", "Your customers"]}>
            {incidents.map((i) => (
              <Tr key={i.incident_id}>
                <Td><span className="font-medium">{i.issuer}</span><div className="text-xs text-muted-foreground">{RAIL[i.rail] ?? i.rail}</div></Td>
                <Td><Badge tone={i.status === "open" ? "bad" : "good"}>{i.status === "open" ? "failing now" : "recovered"}</Badge></Td>
                <Td className="tabular-nums">{pct(i.peak_rate)} <span className="text-xs text-muted-foreground">vs normal {pct(i.baseline_rate)}</span>
                  <div className="mt-1 h-1.5 w-32 rounded-full bg-muted" role="img" aria-label={`peak ${pct(i.peak_rate)}`}>
                    <div className="h-1.5 rounded-full" style={{ width: `${Math.min(100, i.peak_rate * 100)}%`, background: "var(--series-2)" }} />
                  </div></Td>
                <Td className="text-xs text-muted-foreground">{ist(i.started_at)}</Td>
                <Td className="tabular-nums">{duration(i.started_at, i.ended_at)}</Td>
                <Td wrap>{i.yours ? <>{i.yours.affected} hit · {i.yours.contacted} sent a retry link · <b>{i.yours.recovered} paid ({inr(i.yours.recovered_minor)})</b></>
                  : <span className="text-muted-foreground">none of yours</span>}</Td>
              </Tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
