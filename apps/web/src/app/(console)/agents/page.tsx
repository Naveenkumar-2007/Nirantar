import Link from "next/link";
import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, Mono, PageHeader, Table, Td, Tr } from "@/components/kit";

type Action = {
  action_id: string; case_id: string | null; agent_id: string; tool_name: string; status: string;
  policy_decision: string | null; policy_version: string | null; approval_id: string | null; created_at: string;
};
const AGENTS = ["", "conductor", "debit_strategist", "conversation_agent", "verifier", "treasury_agent"];

export default async function AgentsPage({ searchParams }: { searchParams: Promise<{ agent?: string; cursor?: string }> }) {
  const { agent = "", cursor } = await searchParams;
  const qs = new URLSearchParams({ limit: "50" });
  if (agent) qs.set("agent", agent);
  if (cursor) qs.set("cursor", cursor);
  const data = await api<{ items: Action[]; next_cursor: string | null }>(`/v1/agents/activity?${qs}`);
  return (
    <>
      <PageHeader title="AI agents" subtitle="Every tool call an agent made, with the Compliance Guardian's decision" />
      <div className="mb-4 flex flex-wrap gap-2">
        {AGENTS.map((a) => (
          <Link key={a || "all"} href={a ? `/agents?agent=${a}` : "/agents"}
            className={`rounded-full border px-3 py-1 text-xs ${a === agent ? "border-primary text-primary" : "border-border text-muted-foreground"}`}>
            {a || "all agents"}
          </Link>
        ))}
      </div>
      <Card>
        {data.items.length === 0 ? (
          <Empty title="No agent activity yet" />
        ) : (
          <Table head={["When (IST)", "Agent", "Tool", "Policy", "Result", "Case"]}>
            {data.items.map((a) => (
              <Tr key={a.action_id}>
                <Td className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">{ist(a.created_at)}</Td>
                <Td>{a.agent_id}</Td>
                <Td><Mono>{a.tool_name}</Mono></Td>
                <Td>{a.policy_decision ? <Badge>{a.policy_decision}</Badge> : <span className="text-xs text-muted-foreground">read</span>}</Td>
                <Td><Badge>{a.status}</Badge></Td>
                <Td className="text-xs text-muted-foreground">{a.case_id ?? "—"}</Td>
              </Tr>
            ))}
          </Table>
        )}
        {data.next_cursor && (
          <div className="mt-4 text-right text-sm">
            <Link className="text-primary" href={`/agents?${new URLSearchParams({ ...(agent ? { agent } : {}), cursor: data.next_cursor })}`}>Older →</Link>
          </div>
        )}
      </Card>
    </>
  );
}
