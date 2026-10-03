"use client";

import { useActionState } from "react";
import { decideTemplate, proposeTemplate, type FormResult } from "./actions";

function Status({ state }: { state: FormResult | null }) {
  if (!state) return null;
  return <span role="status" className={`text-xs ${state.ok ? "text-[var(--good-fg)]" : "text-[var(--bad-fg)]"}`}>{state.message}</span>;
}

export function ProposeForm({ tkey, language, body, allowed }: { tkey: string; language: string; body: string; allowed: string[] }) {
  const [state, action, pending] = useActionState<FormResult | null, FormData>(proposeTemplate, null);
  return (
    <form action={action} className="space-y-2">
      <input type="hidden" name="key" value={tkey} />
      <input type="hidden" name="language" value={language} />
      <label className="sr-only" htmlFor={`${tkey}-${language}-body`}>New wording</label>
      <textarea id={`${tkey}-${language}-body`} name="body" defaultValue={body} rows={3} required
        className="w-full rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm" />
      <div className="flex flex-wrap items-center gap-2">
        <button disabled={pending} className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-50">
          {pending ? "Checking…" : "Submit for review"}
        </button>
        <span className="text-xs text-[var(--muted)]">Placeholders: {allowed.length ? allowed.map((a) => `{${a}}`).join(" ") : "none"}</span>
        <Status state={state} />
      </div>
    </form>
  );
}

export function ReviewButtons({ tkey, language, version }: { tkey: string; language: string; version: number }) {
  const [state, action, pending] = useActionState<FormResult | null, FormData>(decideTemplate, null);
  return (
    <form action={action} className="flex flex-wrap items-center gap-2">
      <input type="hidden" name="key" value={tkey} />
      <input type="hidden" name="language" value={language} />
      <input type="hidden" name="version" value={version} />
      <button name="grant" value="true" disabled={pending}
        className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">Approve &amp; publish</button>
      <button name="grant" value="false" disabled={pending}
        className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-50">Reject</button>
      <Status state={state} />
    </form>
  );
}
