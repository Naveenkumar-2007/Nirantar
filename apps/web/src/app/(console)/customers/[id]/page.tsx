import Link from "next/link";
import { notFound } from "next/navigation";
import { Bot, CalendarClock, CreditCard, Languages, MessagesSquare, ShieldCheck } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { day, inr, ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader, Table, Td, Tr } from "@/components/kit";
import { Button } from "@/components/ui/button";

type C360 = {
  customer: { customer_id: string; external_ref: string | null; display_name: string | null; preferred_language: string;
    timezone: string; segment: string | null; consents: Record<string, unknown>; has_phone: boolean; has_email: boolean;
    created_at: string };
  subscriptions: { subscription_id: string; provider: string; status: string; amount_minor: number; interval: string | null;
    next_charge_on: string | null; mandate_id: string | null }[];
  mandates: { mandate_id: string; rail: string; status: string; max_amount_minor: number | null; valid_until: string | null;
    failure_reason: string | null; last_verified_at: string | null }[];
  debits: { debit_id: string; scheduled_for: string; amount_minor: number; status: string; attempt_count: number;
    last_error_code: string | null }[];
  cases: { case_id: string; kind: string; status: string; opened_at: string; closed_at: string | null; outcome: string | null }[];
  contacts: { channel: string; purpose: string; status: string; at: string }[];
  actions: { action_id: string; agent_id: string; tool_name: string; status: string; policy_decision: string | null; created_at: string }[];
  replies: { intent: string; promised_date: string | null; created_at: string }[];
};

function Fact({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 text-muted-foreground [&>svg]:size-4">{icon}</span>
      <div><div className="text-xs text-muted-foreground">{label}</div><div className="text-sm">{children}</div></div>
    </div>
  );
}

export default async function CustomerPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let d: C360;
  try {
    d = await api<C360>(`/v1/customers/${encodeURIComponent(id)}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }
  const c = d.customer;
  const optedOut = (c.consents.opted_out as string[] | undefined) ?? [];
  const reachable = ["whatsapp", "sms", "voice", "promotional"].filter((k) => c.consents[k] === true);
  const paid = d.debits.filter((x) => x.status === "succeeded").length;
  return (
    <>
      <PageHeader eyebrow="Customer" title={c.display_name ?? "Customer"} subtitle={c.external_ref ? `Reference ${c.external_ref}` : undefined}
        right={<Button asChild size="sm"><Link href={`/conversations?c=${c.customer_id}`}><MessagesSquare />Conversation</Link></Button>} />
      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Profile">
          <div className="space-y-4">
            <Fact icon={<Languages />} label="Language · timezone">{c.preferred_language} · {c.timezone}</Fact>
            <Fact icon={<ShieldCheck />} label="Consent">
              <div className="mt-1 flex flex-wrap gap-1">
                {reachable.map((k) => <Badge key={k} tone={optedOut.includes(k) ? "warn" : "good"}>{k}{optedOut.includes(k) ? " (opted out)" : ""}</Badge>)}
                {reachable.length === 0 && <span className="text-muted-foreground">No contact consent recorded</span>}
              </div>
            </Fact>
            <Fact icon={<CalendarClock />} label="Customer since">{day(c.created_at)}</Fact>
            <Fact icon={<CreditCard />} label="Payment record">{paid} of {d.debits.length} recent debits paid</Fact>
            <p className="text-xs text-muted-foreground">Phone and email are stored encrypted{c.has_phone ? "" : " (no phone on file)"}.</p>
          </div>
        </Card>

        <Card title="Subscriptions & mandates" className="xl:col-span-2">
          {d.subscriptions.length === 0 ? <Empty title="No subscriptions" /> : (
            <div className="space-y-3">
              {d.subscriptions.map((s) => {
                const m = d.mandates.find((x) => x.mandate_id === s.mandate_id);
                return (
                  <div key={s.subscription_id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3">
                    <div>
                      <div className="flex items-center gap-2"><span className="font-medium">{inr(s.amount_minor)}</span>
                        <span className="text-xs text-muted-foreground">/ {s.interval ?? "cycle"} · {s.provider}</span><Badge>{s.status}</Badge></div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        {s.next_charge_on ? `Next charge ${day(s.next_charge_on)}` : "No upcoming charge"}
                      </div>
                    </div>
                    <div className="text-right text-xs">
                      {m ? (<><div><Badge>{m.status}</Badge> <span className="text-muted-foreground">{m.rail.replace("_", " ")}</span></div>
                        <div className="mt-1 text-muted-foreground">
                          {m.max_amount_minor ? `limit ${inr(m.max_amount_minor)}` : "limit unknown"}{m.valid_until ? ` · valid to ${day(m.valid_until)}` : ""}
                        </div></>) : <span className="text-muted-foreground">No mandate on record</span>}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Card>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <Card title="Debits" description="Most recent first · open a date for its full timeline">
          {d.debits.length === 0 ? <Empty title="No debits" /> : (
            <Table head={["Due", "Amount", "Status", "Tries"]}>
              {d.debits.map((x) => (
                <Tr key={x.debit_id}>
                  <Td><Link href={`/debits/${x.debit_id}`} className="text-primary hover:underline">{day(x.scheduled_for)}</Link></Td><Td className="tabular-nums">{inr(x.amount_minor)}</Td>
                  <Td><Badge>{x.status}</Badge>{x.last_error_code && <span className="ml-2 text-xs text-muted-foreground">{x.last_error_code}</span>}</Td>
                  <Td className="tabular-nums">{x.attempt_count}</Td>
                </Tr>
              ))}
            </Table>
          )}
        </Card>
        <Card title="What Nirantar did" description="Agent actions for this customer, with policy decisions">
          {d.actions.length === 0 ? <Empty icon={<Bot />} title="No agent actions yet" /> : (
            <ol className="relative space-y-3 border-l border-border pl-4">
              {d.actions.map((a) => (
                <li key={a.action_id}>
                  <span className="absolute -left-[5px] mt-1.5 size-2.5 rounded-full border-2 border-card bg-primary" />
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <Mono>{a.tool_name}</Mono><Badge>{a.status}</Badge>
                    {a.policy_decision && <Badge tone="neutral">{a.policy_decision.toLowerCase()}</Badge>}
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">{a.agent_id.replaceAll("_", " ")} · {ist(a.created_at)}</div>
                </li>
              ))}
            </ol>
          )}
        </Card>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <Card title="Cases">
          {d.cases.length === 0 ? <Empty title="No cases" /> : (
            <Table head={["Kind", "Status", "Opened", "Outcome"]}>
              {d.cases.map((x) => (
                <Tr key={x.case_id}><Td>{x.kind.replace("_", " ")}</Td><Td><Badge>{x.status}</Badge></Td>
                  <Td className="text-xs text-muted-foreground">{ist(x.opened_at)}</Td><Td>{x.outcome ?? "—"}</Td></Tr>
              ))}
            </Table>
          )}
        </Card>
        <Card title="Contacts & replies">
          {d.contacts.length === 0 && d.replies.length === 0 ? <Empty title="No contact yet" /> : (
            <div className="space-y-2 text-sm">
              {d.replies.map((r, i) => (
                <div key={`r${i}`} className="flex items-center justify-between gap-2">
                  <span><Badge tone="info">replied: {r.intent.replaceAll("_", " ")}</Badge>{r.promised_date && <span className="ml-2 text-xs text-muted-foreground">promised {day(r.promised_date)}</span>}</span>
                  <span className="text-xs text-muted-foreground">{ist(r.created_at)}</span>
                </div>
              ))}
              {d.contacts.map((x, i) => (
                <div key={`c${i}`} className="flex items-center justify-between gap-2">
                  <span><Badge tone="neutral" mark={false}>{x.channel}</Badge> <span className="text-muted-foreground">{x.purpose}</span></span>
                  <span className="text-xs text-muted-foreground">{ist(x.at)}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
