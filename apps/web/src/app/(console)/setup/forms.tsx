"use client";

import { useActionState, useEffect, useTransition } from "react";
import { useRouter } from "next/navigation";
import { connectRazorpay, startImport, type ConnectResult, type ImportResult } from "./actions";

const input =
  "mt-1 block w-full rounded-md border border-border bg-transparent px-3 py-2 font-mono text-sm outline-none focus:border-ring";

export function RazorpayForm() {
  const [state, action, pending] = useActionState<ConnectResult | null, FormData>(connectRazorpay, null);
  if (state?.ok && state.webhook) {
    return (
      <div className="space-y-3">
        <p className="text-sm font-medium text-success">✓ {state.message}. Last step — add the webhook in Razorpay:</p>
        <ol className="list-decimal space-y-2 pl-5 text-sm">
          <li>Razorpay Dashboard → <b>Account &amp; Settings → Webhooks → Add New Webhook</b></li>
          <li>Webhook URL: <code className="break-all rounded bg-muted px-1.5 py-0.5 text-xs">{state.webhook.url}</code></li>
          <li>Secret (shown only now — copy it):{" "}
            <code className="break-all rounded bg-muted px-1.5 py-0.5 text-xs">{state.webhook.secret}</code></li>
          <li>Active events: <span className="text-xs text-muted-foreground">{state.webhook.events.join(", ")}</span></li>
        </ol>
        <p className="text-xs text-muted-foreground">
          Razorpay can only reach this URL once Nirantar is on a public address. Until then, Nirantar still sees your
          payments through the import and the 30-minute reconciliation.
        </p>
      </div>
    );
  }
  return (
    <form action={action} className="space-y-3">
      <label className="block">
        <span className="text-sm font-medium">Key id</span>
        <input name="key_id" required placeholder="rzp_test_…" autoComplete="off" spellCheck={false} className={input} />
      </label>
      <label className="block">
        <span className="text-sm font-medium">Key secret</span>
        <input name="key_secret" type="password" required autoComplete="off" spellCheck={false} className={input} />
      </label>
      <p className="text-xs text-muted-foreground">
        Razorpay → Account &amp; Settings → API keys. Nirantar checks them with Razorpay, then stores them encrypted
        for your business only. Test keys here; live keys in production.
      </p>
      <button disabled={pending}
        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
        {pending ? "Checking with Razorpay…" : "Connect Razorpay"}
      </button>
      {state && !state.ok && <p role="alert" className="text-sm text-danger">{state.message}</p>}
    </form>
  );
}

export function ImportButton({ label }: { label: string }) {
  const [state, action, pending] = useActionState<ImportResult | null, FormData>(async () => startImport(), null);
  return (
    <form action={action} className="flex flex-wrap items-center gap-3">
      <button disabled={pending}
        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
        {pending ? "Starting…" : label}
      </button>
      {state && <span className={`text-sm ${state.ok ? "text-success" : "text-danger"}`}>{state.message}</span>}
    </form>
  );
}

/** While the import runs, re-render the page every few seconds so progress shows without a manual refresh. */
export function AutoRefresh({ every = 5000 }: { every?: number }) {
  const router = useRouter();
  const [, start] = useTransition();
  useEffect(() => {
    const id = setInterval(() => start(() => router.refresh()), every);
    return () => clearInterval(id);
  }, [router, every]);
  return null;
}
