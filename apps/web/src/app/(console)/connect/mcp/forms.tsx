"use client";

import { useActionState } from "react";
import { decideConnection, revokeConnection, type ConnectResult } from "./actions";

const LABELS: Record<string, { title: string; detail: string }> = {
  "nirantar:read": { title: "Read", detail: "Overview, open cases, customer profiles, templates, ledger checks." },
  "nirantar:act": {
    title: "Act on cases",
    detail: "Message customers and update cases. Consent, contact hours and fatigue rules still apply.",
  },
  "nirantar:money": {
    title: "Propose money actions",
    detail: "Payment links, dispute submissions, discount offers. Every one waits for your approval in Approvals.",
  },
};

export function ConsentForm({ requestId, scopes }: { requestId: string; scopes: string[] }) {
  const [state, action, pending] = useActionState<ConnectResult | null, FormData>(decideConnection, null);
  return (
    <form action={action} className="space-y-4">
      <input type="hidden" name="request_id" value={requestId} />
      <fieldset className="space-y-3">
        <legend className="text-sm font-medium">Permissions it asked for</legend>
        {scopes.map((s) => (
          <label key={s} className="flex items-start gap-3 rounded-md border border-border p-3">
            <input type="checkbox" name="scope" value={s} defaultChecked={s === "nirantar:read"} className="mt-1" />
            <span>
              <span className="block text-sm font-medium">{LABELS[s]?.title ?? s}</span>
              <span className="block text-xs text-muted-foreground">{LABELS[s]?.detail}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <div className="flex flex-wrap items-center gap-2">
        <button name="approve" value="true" disabled={pending}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50">
          Connect
        </button>
        <button name="approve" value="false" disabled={pending}
          className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50">
          Refuse
        </button>
        {state && !state.ok && <span className="text-xs text-danger">{state.message}</span>}
      </div>
    </form>
  );
}

export function RevokeButton({ grantId }: { grantId: string }) {
  const [state, action, pending] = useActionState<ConnectResult | null, FormData>(revokeConnection, null);
  return (
    <form action={action} className="flex items-center gap-2">
      <input type="hidden" name="grant_id" value={grantId} />
      <button disabled={pending}
        className="rounded-md border border-border px-3 py-1 text-xs disabled:opacity-50">
        Disconnect
      </button>
      {state && <span className={`text-xs ${state.ok ? "text-success" : "text-danger"}`}>{state.message}</span>}
    </form>
  );
}
