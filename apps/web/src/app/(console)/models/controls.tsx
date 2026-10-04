"use client";

import { useActionState } from "react";
import { retireVersion, trainNow, type ActionResult } from "./actions";

function Status({ s }: { s: ActionResult | null }) {
  if (!s) return null;
  return <span role="status" className={`text-xs ${s.ok ? "text-success" : "text-danger"}`}>{s.message}</span>;
}

export function TrainButton() {
  const [s, action, pending] = useActionState<ActionResult | null, FormData>(trainNow, null);
  return (
    <form action={action} className="flex items-center gap-2">
      <button disabled={pending} className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50">
        {pending ? "Starting…" : "Train now"}
      </button>
      <Status s={s} />
    </form>
  );
}

export function RetireForm({ version }: { version: string }) {
  const [s, action, pending] = useActionState<ActionResult | null, FormData>(retireVersion, null);
  return (
    <form action={action} className="flex flex-wrap items-center gap-2">
      <input type="hidden" name="version" value={version} />
      <label className="sr-only" htmlFor={`retire-${version}`}>Reason</label>
      <input id={`retire-${version}`} name="reason" required minLength={3} placeholder="Reason to retire"
        className="w-44 rounded-md border border-border bg-background px-2 py-1 text-xs" />
      <button disabled={pending} className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50">Retire</button>
      <Status s={s} />
    </form>
  );
}
