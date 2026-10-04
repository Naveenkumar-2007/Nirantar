import Link from "next/link";
import { Check, CheckCheck, Clock, FileText, Image as ImageIcon, LayoutTemplate, MessagesSquare, Mic, TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Empty, PageHeader } from "@/components/kit";

type Item = { customer_id: string; display_name: string | null; preferred_language: string; direction: string; kind: string;
  status: string; last_at: string; last_inbound_at: string | null; messages: number };
type Msg = { message_id: string; direction: "inbound" | "outbound"; kind: string; template_ref: string | null; status: string;
  error_title: string | null; evidence_id: string | null; created_at: string; text: string | null };
type Thread = { customer_id: string; display_name: string | null; language: string; window_open_until: string | null; messages: Msg[] };

/* delivery state: icon + word, never colour alone */
function Delivery({ status, error }: { status: string; error: string | null }) {
  const map: Record<string, [React.ReactNode, string, string]> = {
    sent: [<Check key="i" />, "sent", "text-muted-foreground"],
    delivered: [<CheckCheck key="i" />, "delivered", "text-muted-foreground"],
    read: [<CheckCheck key="i" />, "read", "text-info"],
    failed: [<TriangleAlert key="i" />, error ? `failed — ${error}` : "failed", "text-danger"],
  };
  const [icon, label, cls] = map[status] ?? [<Clock key="i" />, status, "text-muted-foreground"];
  return <span className={cn("inline-flex items-center gap-1 text-[11px] [&>svg]:size-3.5", cls)}>{icon}{label}</span>;
}

function KindTag({ m }: { m: Msg }) {
  if (m.kind === "audio") return <span className="inline-flex items-center gap-1 text-[11px] font-medium [&>svg]:size-3.5"><Mic />{m.direction === "inbound" ? "Voice note · transcript" : "Spoken reply"}</span>;
  if (m.kind === "image") return <span className="inline-flex items-center gap-1 text-[11px] font-medium [&>svg]:size-3.5"><ImageIcon />Image · checked as payment evidence</span>;
  if (m.kind === "document") return <span className="inline-flex items-center gap-1 text-[11px] font-medium [&>svg]:size-3.5"><FileText />Document</span>;
  if (m.kind === "template") return <span className="inline-flex items-center gap-1 text-[11px] font-medium [&>svg]:size-3.5"><LayoutTemplate />Approved template {m.template_ref ? `· ${m.template_ref}` : ""}</span>;
  return null;
}

export default async function ConversationsPage({ searchParams }: { searchParams: Promise<{ c?: string }> }) {
  const { c } = await searchParams;
  const list = await api<{ items: Item[] }>("/v1/conversations");
  const selected = c ?? list.items[0]?.customer_id;
  const thread = selected ? await api<Thread>(`/v1/conversations/${encodeURIComponent(selected)}`).catch(() => null) : null;
  const open = thread?.window_open_until && new Date(thread.window_open_until) > new Date();
  return (
    <>
      <PageHeader eyebrow="Operate" title="Conversations"
        subtitle="Every WhatsApp message Nirantar sent or received, with voice notes transcribed. Message text is encrypted for your business." />
      {list.items.length === 0 ? (
        <Empty icon={<MessagesSquare />} title="No conversations yet"
          hint="Messages appear here once Nirantar contacts customers on WhatsApp or they write to your number." />
      ) : (
        <div className="grid min-h-[60vh] overflow-hidden rounded-xl border border-border bg-card shadow-card md:grid-cols-[18rem_1fr]">
          <ul className="max-h-[70vh] divide-y divide-border overflow-y-auto border-b border-border md:border-b-0 md:border-r">
            {list.items.map((it) => (
              <li key={it.customer_id}>
                <Link href={`/conversations?c=${it.customer_id}`}
                  className={cn("block px-4 py-3 hover:bg-accent/60", it.customer_id === selected && "bg-accent")}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{it.display_name ?? "Customer"}</span>
                    <span className="shrink-0 text-[11px] text-muted-foreground">{ist(it.last_at)}</span>
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {it.direction === "inbound" ? "Customer wrote" : "Nirantar sent"} · {it.kind} · {it.messages} messages
                  </div>
                </Link>
              </li>
            ))}
          </ul>
          <section className="flex min-w-0 flex-col">
            {!thread ? <div className="p-6"><Empty title="Choose a conversation" /></div> : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-5 py-3">
                  <div>
                    <Link href={`/customers/${thread.customer_id}`} className="font-medium hover:underline">{thread.display_name ?? "Customer"}</Link>
                    <div className="text-xs text-muted-foreground">Language: {thread.language}</div>
                  </div>
                  <span className={cn("rounded-full px-2.5 py-1 text-xs", open ? "bg-success-soft text-success" : "bg-muted text-muted-foreground")}>
                    {open ? `Reply window open until ${ist(thread.window_open_until)}` : "Outside the 24-hour window — only approved templates"}
                  </span>
                </div>
                <div className="flex-1 space-y-3 overflow-y-auto bg-background/40 p-5">
                  {thread.messages.map((m) => (
                    <div key={m.message_id} className={cn("flex", m.direction === "outbound" ? "justify-end" : "justify-start")}>
                      <div className={cn("max-w-[75%] rounded-2xl px-4 py-2.5 shadow-card",
                        m.direction === "outbound" ? "rounded-br-sm bg-primary/10 ring-1 ring-primary/20" : "rounded-bl-sm bg-card ring-1 ring-border")}>
                        <KindTag m={m} />
                        <p className="whitespace-pre-wrap text-sm leading-relaxed">{m.text ?? <span className="italic text-muted-foreground">(no text)</span>}</p>
                        <div className="mt-1 flex items-center justify-end gap-2 text-[11px] text-muted-foreground">
                          <span>{ist(m.created_at)}</span>
                          {m.direction === "outbound" && <Delivery status={m.status} error={m.error_title} />}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </>
  );
}
