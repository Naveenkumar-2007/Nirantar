"use client";

import { useActionState } from "react";
import { decideApproval, type DecideResult } from "./actions";

export function DecideButtons({ approvalId }: { approvalId: string }) {
  const [state, action, pending] = useActionState<DecideResult | null, FormData>(decideApproval, null);
  return (
    <form action={action} className="flex flex-wrap items-center gap-2">
      <input type="hidden" name="approval_id" value={approvalId} />
      <button name="grant" value="true" disabled={pending}
        className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50">
        Approve
      </button>
      <button name="grant" value="false" disabled={pending}
        className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50">
        Deny
      </button>
      {state && <span className={`text-xs ${state.ok ? "text-success" : "text-danger"}`}>{state.message}</span>}
    </form>
  );
}
