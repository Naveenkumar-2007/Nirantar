"use client";

import { useActionState } from "react";
import { refreshRetention, type RefreshResult } from "./actions";

export function RefreshButton() {
  const [s, action, pending] = useActionState<RefreshResult | null, FormData>(refreshRetention, null);
  return (
    <form action={action} className="flex items-center gap-2">
      <button disabled={pending} className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50">
        {pending ? "Starting…" : "Refresh now"}
      </button>
      {s && <span role="status" className={`text-xs ${s.ok ? "text-success" : "text-danger"}`}>{s.message}</span>}
    </form>
  );
}
