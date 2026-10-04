"use client";

import { useActionState } from "react";
import { syncNow, type SyncResult } from "./actions";

export function SyncButton() {
  const [state, action, pending] = useActionState<SyncResult | null, FormData>(syncNow, null);
  return (
    <form action={action} className="flex flex-wrap items-center gap-2">
      <button disabled={pending}
        className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50">
        {pending ? "Starting…" : "Sync now"}
      </button>
      {state && <span role="status" className={`text-xs ${state.ok ? "text-success" : "text-danger"}`}>{state.message}</span>}
    </form>
  );
}
