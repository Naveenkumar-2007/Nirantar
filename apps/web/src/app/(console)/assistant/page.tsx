"use client";

import { useState } from "react";

type Answer = {
  status?: string; answer?: string; error?: string;
  citations?: { chunk_id: string; doc_id: string; quote: string; source_uri: string }[];
};

const EXAMPLES = [
  "Between what hours may a lender's recovery agent contact a borrower?",
  "How many hours before a recurring debit must the customer be notified?",
  "Does a reminder with a discount count as promotional under TRAI rules?",
];

export default function AssistantPage() {
  const [q, setQ] = useState(EXAMPLES[0]);
  const [res, setRes] = useState<Answer | null>(null);
  const [busy, setBusy] = useState(false);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setRes(null);
    try {
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }) });
      setRes(await r.json());
    } catch {
      setRes({ error: "network error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="mb-6">
        <h1 className="text-xl font-semibold tracking-tight">Policy assistant</h1>
        <p className="mt-1 text-sm text-muted-foreground">Answers only from the regulatory knowledge base, with verbatim citations. If the evidence isn&apos;t there, it says so. Engineering aid — not legal advice.</p>
      </div>
      <form onSubmit={ask} className="rounded-xl border border-border bg-card p-4">
        <textarea value={q} onChange={(e) => setQ(e.target.value)} rows={3}
          className="w-full rounded-md border border-border bg-transparent p-2 text-sm" />
        <div className="mt-2 flex flex-wrap gap-2">
          {EXAMPLES.map((x) => (
            <button type="button" key={x} onClick={() => setQ(x)} className="rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">{x}</button>
          ))}
        </div>
        <button disabled={busy} className="mt-3 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50">
          {busy ? "Searching…" : "Ask"}
        </button>
      </form>
      {res && (
        <div className="mt-4 rounded-xl border border-border bg-card p-4">
          {res.error && <p className="text-sm text-danger">{res.error}</p>}
          {res.status === "insufficient_evidence" && <p className="text-sm">The knowledge base doesn&apos;t support an answer to this question.</p>}
          {res.answer && <p className="text-sm leading-6">{res.answer}</p>}
          {res.citations && res.citations.length > 0 && (
            <ul className="mt-3 space-y-2">
              {res.citations.map((c) => (
                <li key={c.chunk_id} className="rounded-md bg-muted p-2 text-xs">
                  <div className="font-mono">{c.doc_id}</div>
                  <blockquote className="mt-1 italic">“{c.quote}”</blockquote>
                  {c.source_uri.startsWith("http") && <a className="mt-1 inline-block text-primary" href={c.source_uri} target="_blank" rel="noreferrer">source ↗</a>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}
