import Link from "next/link";
import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Mono, PageHeader } from "@/components/kit";
import { ProposeForm, ReviewButtons } from "./forms";

type Version = {
  template_key: string; language: string; version: number; body: string; status: string;
  created_by: string; created_at: string; decided_by: string | null; decided_at: string | null;
};
type Item = {
  key: string; language: string; channel: string; mandatory: boolean; required: string[]; allowed: string[];
  live: { version: number; source: string; body: string; language: string }; versions: Version[];
};

const LANG: Record<string, string> = { en: "English", hi: "Hindi", te: "Telugu" };

export default async function TemplatesPage() {
  const data = await api<{ items: Item[]; can_propose: boolean; can_review: boolean }>("/v1/templates");
  const groups = new Map<string, Item[]>();
  for (const i of data.items) groups.set(i.key, [...(groups.get(i.key) ?? []), i]);
  const pending = data.items.flatMap((i) => i.versions.filter((v) => v.status === "pending").map((v) => ({ i, v })));

  return (
    <>
      <PageHeader title="Message templates"
        subtitle="What customers read and hear. New wording goes live only after automated compliance checks and a second person's approval."
        right={<Link href="/automations" className="rounded-md border border-border px-3 py-1.5 text-sm">← Automations</Link>} />

      {pending.length > 0 && (
        <Card title={`Waiting for review (${pending.length})`} className="mb-4">
          <ul className="space-y-3">
            {pending.map(({ i, v }) => (
              <li key={`${v.template_key}-${v.language}-${v.version}`} className="rounded-lg border border-border p-3">
                <div className="flex flex-wrap items-center gap-2 text-sm"><Mono>{i.key}</Mono><Badge tone="info">{LANG[i.language]}</Badge>
                  <Badge>pending</Badge><span className="text-xs text-muted-foreground">v{v.version} by {v.created_by} · {ist(v.created_at)}</span></div>
                <div className="mt-2 grid gap-2 text-sm md:grid-cols-2">
                  <div><div className="text-xs text-muted-foreground">Live now (v{i.live.version})</div><p className="mt-1 whitespace-pre-wrap">{i.live.body}</p></div>
                  <div><div className="text-xs text-muted-foreground">Proposed</div><p className="mt-1 whitespace-pre-wrap">{v.body}</p></div>
                </div>
                {data.can_review && <div className="mt-2"><ReviewButtons tkey={i.key} language={i.language} version={v.version} /></div>}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="space-y-4">
        {[...groups.entries()].map(([key, items]) => (
          <Card key={key}>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Mono>{key}</Mono><Badge tone="neutral">{items[0].channel}</Badge>
              {items[0].mandatory && <Badge tone="warn">mandatory notice</Badge>}
            </div>
            <div className="grid gap-4 lg:grid-cols-3">
              {items.map((i) => (
                <div key={i.language} className="space-y-2">
                  <div className="flex items-center gap-2 text-sm font-medium">{LANG[i.language]}
                    {i.live.source === "tenant" ? <Badge tone="good">yours · v{i.live.version}</Badge> : <Badge tone="neutral">platform default</Badge>}
                  </div>
                  {data.can_propose
                    ? <ProposeForm tkey={i.key} language={i.language} body={i.live.body} allowed={i.allowed} />
                    : <p className="whitespace-pre-wrap text-sm">{i.live.body}</p>}
                  {i.versions.filter((v) => v.status !== "pending").slice(0, 3).map((v) => (
                    <p key={v.version} className="text-xs text-muted-foreground">v{v.version} {v.status}{v.decided_by ? ` by ${v.decided_by}` : ""} · {ist(v.decided_at ?? v.created_at)}</p>
                  ))}
                </div>
              ))}
            </div>
          </Card>
        ))}
      </div>
    </>
  );
}
