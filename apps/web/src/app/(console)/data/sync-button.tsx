"use client";

import { useActionState } from "react";
import { syncNow, type SyncResult } from "./actions";

export function SyncButton() {
  const [state, action, pending] = useActionState<SyncResult | null, FormData>(syncNow, null);
  return (
    <form action={action} className="flex flex-wrap items-center gap-2">
      <button disabled={pending}
        className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
        {pending ? "Starting…" : "Sync now"}
      </button>
      {state && <span role="status" className={`text-xs ${state.ok ? "text-[var(--good-fg)]" : "text-[var(--bad-fg)]"}`}>{state.message}</span>}
    </form>
  );
}
