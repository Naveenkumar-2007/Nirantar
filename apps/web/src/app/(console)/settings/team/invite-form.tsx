"use client";

import { useActionState } from "react";
import { inviteMember, type TeamResult } from "./actions";

const ROLES: [string, string][] = [
  ["viewer", "Viewer — sees everything, changes nothing"],
  ["ops_analyst", "Operations — customers and subscriptions"],
  ["finance_approver", "Finance — approves money actions, reads audit"],
  ["compliance_officer", "Compliance — policies, audit, agent controls"],
  ["owner", "Owner — full control"],
];

export function InviteForm() {
  const [state, action, pending] = useActionState<TeamResult | null, FormData>(inviteMember, null);
  return (
    <form action={action} className="flex flex-wrap items-end gap-3">
      <label className="min-w-56 flex-1">
        <span className="text-sm font-medium">Email</span>
        <input name="email" type="email" required autoComplete="off"
          className="mt-1 block w-full rounded-md border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-ring" />
      </label>
      <label className="min-w-56">
        <span className="text-sm font-medium">Role</span>
        <select name="role" defaultValue="viewer"
          className="mt-1 block w-full rounded-md border border-border bg-card px-3 py-2 text-sm">
          {ROLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </label>
      <button disabled={pending} className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
        {pending ? "Inviting…" : "Invite"}
      </button>
      {state && <p role="status" className={`w-full text-sm ${state.ok ? "text-success" : "text-danger"}`}>{state.message}</p>}
    </form>
  );
}
