"use client";

import { useActionState } from "react";
import { createBusiness, type CreateResult } from "./actions";

const SEGMENTS: [string, string][] = [
  ["subscription", "Consumer subscriptions (D2C, boxes, memberships)"],
  ["saas", "Software / SaaS"],
  ["edtech", "Education"],
  ["media", "Media & content"],
  ["fitness", "Fitness & wellness"],
  ["insurance", "Insurance premiums"],
  ["lending", "Lending / EMIs"],
  ["other", "Something else"],
];

export function BusinessForm() {
  const [state, action, pending] = useActionState<CreateResult | null, FormData>(createBusiness, null);
  return (
    <form action={action} className="space-y-4">
      <label className="block">
        <span className="text-sm font-medium">Business name</span>
        <input name="name" required minLength={2} maxLength={80} autoComplete="organization"
          className="mt-1 block w-full rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]" />
      </label>
      <label className="block">
        <span className="text-sm font-medium">What do you sell?</span>
        <select name="segment" defaultValue="subscription"
          className="mt-1 block w-full rounded-md border border-[var(--border)] bg-[var(--card)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]">
          {SEGMENTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </label>
      <button disabled={pending}
        className="w-full rounded-md bg-[var(--accent)] px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
        {pending ? "Creating…" : "Create business"}
      </button>
      {state && !state.ok && <p role="alert" className="text-sm text-[var(--bad-fg)]">{state.message}</p>}
    </form>
  );
}
