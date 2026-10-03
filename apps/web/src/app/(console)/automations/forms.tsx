"use client";

import type { ReactNode } from "react";
import { useActionState } from "react";
import { refreshLearned, saveSettings, type FormResult } from "./actions";

function Status({ state }: { state: FormResult | null }) {
  if (!state) return null;
  return (
    <span role="status" className={`text-xs ${state.ok ? "text-[var(--good-fg)]" : "text-[var(--bad-fg)]"}`}>
      {state.message}
    </span>
  );
}

/** Wraps one settings section. Every save needs a reason and the version being edited (optimistic concurrency:
 * if someone else saved first, the API answers 409 and nothing is overwritten). */
export function SettingsForm({ namespace, version, canEdit, children }: {
  namespace: string; version: number; canEdit: boolean; children: ReactNode;
}) {
  const [state, action, pending] = useActionState<FormResult | null, FormData>(saveSettings, null);
  return (
    <form action={action} className="space-y-4">
      <input type="hidden" name="namespace" value={namespace} />
      <input type="hidden" name="version" value={version} />
      <fieldset disabled={!canEdit || pending} className="space-y-4 disabled:opacity-70">{children}</fieldset>
      {canEdit ? (
        <div className="flex flex-wrap items-center gap-2 border-t border-[var(--border)] pt-3">
          <label className="sr-only" htmlFor={`${namespace}-reason`}>Reason for change</label>
          <input id={`${namespace}-reason`} name="reason" required minLength={3} placeholder="Reason for this change (audited)"
            className="min-w-0 flex-1 rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
          <button disabled={pending}
            className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
            {pending ? "Saving…" : "Save new version"}
          </button>
          <Status state={state} />
        </div>
      ) : (
        <p className="text-xs text-[var(--muted)]">Read-only: your role cannot change settings (needs policy:admin).</p>
      )}
    </form>
  );
}

export function RefreshLearnedButton({ canEdit }: { canEdit: boolean }) {
  const [state, action, pending] = useActionState<FormResult | null, FormData>(refreshLearned, null);
  if (!canEdit) return null;
  return (
    <form action={action} className="flex items-center gap-2">
      <button disabled={pending}
        className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-50">
        {pending ? "Learning…" : "Re-learn from verified outcomes"}
      </button>
      <Status state={state} />
    </form>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="text-sm font-medium text-[var(--fg)]">{label}</span>
      {hint && <span className="mt-0.5 block text-xs text-[var(--muted)]">{hint}</span>}
      <span className="mt-1 block">{children}</span>
    </label>
  );
}

export const inputCls = "w-full rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm tabular-nums";
