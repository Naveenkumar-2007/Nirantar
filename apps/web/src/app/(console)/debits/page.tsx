import Link from "next/link";
import { api } from "@/lib/api";
import { day, inr } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Table, Td } from "@/components/ui";

type Debit = {
  debit_id: string; customer_id: string; display_name: string | null; scheduled_for: string; amount_minor: number;
  status: string; attempt_count: number; last_error_code: string | null;
};
const STATUSES = ["", "scheduled", "notified", "attempting", "succeeded", "failed"];

export default async function DebitsPage({ searchParams }: { searchParams: Promise<{ status?: string; cursor?: string }> }) {
  const { status = "", cursor } = await searchParams;
  const qs = new URLSearchParams({ limit: "25" });
  if (status) qs.set("status", status);
  if (cursor) qs.set("cursor", cursor);
  const data = await api<{ items: Debit[]; next_cursor: string | null }>(`/v1/debits?${qs}`);
  return (
    <>
      <PageHeader title="Debits" subtitle="Every scheduled debit and where it is in its cycle" />
      <div className="mb-4 flex flex-wrap gap-2">
        {STATUSES.map((s) => (
          <Link key={s || "all"} href={s ? `/debits?status=${s}` : "/debits"}
            className={`rounded-full border px-3 py-1 text-xs ${s === status ? "border-[var(--accent)] text-[var(--accent)]" : "border-[var(--border)] text-[var(--muted)]"}`}>
            {s || "all"}
          </Link>
        ))}
      </div>
      <Card>
        {data.items.length === 0 ? (
          <Empty title="No debits match" hint="Try another status filter." />
        ) : (
          <Table head={["Customer", "Due", "Amount", "Status", "Attempts", "Last error", ""]}>
            {data.items.map((d) => (
              <tr key={d.debit_id}>
                <Td>{d.display_name ?? d.customer_id}</Td>
                <Td>{day(d.scheduled_for)}</Td>
                <Td className="tabular-nums">{inr(d.amount_minor)}</Td>
                <Td><Badge>{d.status}</Badge></Td>
                <Td className="tabular-nums">{d.attempt_count}</Td>
                <Td className="text-xs text-[var(--muted)]">{d.last_error_code ?? "—"}</Td>
                <Td><Link className="text-[var(--accent)]" href={`/debits/${d.debit_id}`}>Timeline →</Link></Td>
              </tr>
            ))}
          </Table>
        )}
        <div className="mt-4 flex justify-between text-sm">
          {cursor ? <Link className="text-[var(--accent)]" href={status ? `/debits?status=${status}` : "/debits"}>← First page</Link> : <span />}
          {data.next_cursor && (
            <Link className="text-[var(--accent)]" href={`/debits?${new URLSearchParams({ ...(status ? { status } : {}), cursor: data.next_cursor })}`}>
              Next page →
            </Link>
          )}
        </div>
      </Card>
    </>
  );
}
