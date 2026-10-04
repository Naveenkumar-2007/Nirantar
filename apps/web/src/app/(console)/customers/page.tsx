import Link from "next/link";
import { Users } from "lucide-react";
import { api } from "@/lib/api";
import { day } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Table, Td, Tr } from "@/components/kit";
import { Button } from "@/components/ui/button";

type Customer = {
  customer_id: string; external_ref: string | null; display_name: string | null; preferred_language: string;
  segment: string | null; consents: Record<string, unknown>; has_phone: boolean; created_at: string;
};
type Page = { items: Customer[]; next_cursor: string | null };

const LANG: Record<string, string> = { en: "English", hi: "हिन्दी", te: "తెలుగు", ta: "தமிழ்", kn: "ಕನ್ನಡ", mr: "मराठी" };

function channels(consents: Record<string, unknown>): string[] {
  const out = (consents.opted_out as string[] | undefined) ?? [];
  return ["whatsapp", "sms", "voice"].filter((c) => consents[c] === true && !out.includes(c));
}

export default async function CustomersPage({ searchParams }: { searchParams: Promise<{ cursor?: string }> }) {
  const { cursor } = await searchParams;
  const page = await api<Page>(`/v1/customers?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
  return (
    <>
      <PageHeader eyebrow="Operate" title="Customers"
        subtitle="Everyone with a subscription or EMI. Contact details stay encrypted — Nirantar shows only how each person can be reached." />
      <Card>
        {page.items.length === 0 ? (
          <Empty icon={<Users />} title="No customers yet" hint="They arrive with your history import or the first payment." />
        ) : (
          <Table head={["Customer", "Reference", "Language", "Can be reached on", "Since", ""]}>
            {page.items.map((c) => (
              <Tr key={c.customer_id}>
                <Td><Link href={`/customers/${c.customer_id}`} className="font-medium hover:underline">{c.display_name ?? "—"}</Link></Td>
                <Td className="text-xs text-muted-foreground">{c.external_ref ?? "—"}</Td>
                <Td>{LANG[c.preferred_language] ?? c.preferred_language}</Td>
                <Td>
                  <div className="flex gap-1">
                    {channels(c.consents).map((ch) => <Badge key={ch} tone="neutral" mark={false}>{ch}</Badge>)}
                    {(c.consents.opted_out as string[] | undefined)?.length ? <Badge tone="warn">opted out</Badge> : null}
                  </div>
                </Td>
                <Td className="text-xs text-muted-foreground">{day(c.created_at)}</Td>
                <Td><Link href={`/customers/${c.customer_id}`} className="text-sm text-primary">Open →</Link></Td>
              </Tr>
            ))}
          </Table>
        )}
        {page.next_cursor && (
          <div className="mt-4 flex justify-end">
            <Button asChild variant="outline" size="sm"><Link href={`/customers?cursor=${encodeURIComponent(page.next_cursor)}`}>Next page</Link></Button>
          </div>
        )}
      </Card>
    </>
  );
}
