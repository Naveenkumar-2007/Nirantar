"use client";

export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-6">
      <h2 className="text-base font-semibold">Couldn&apos;t load this page</h2>
      <p className="mt-1 text-sm text-[var(--muted)]">
        The Nirantar API returned an error. Check that the API is running and the dashboard&apos;s API key is valid.
      </p>
      <pre className="mt-3 overflow-x-auto rounded bg-[var(--chip)] p-3 text-xs">{error.message}</pre>
      <button onClick={reset} className="mt-4 rounded-md bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-white">
        Try again
      </button>
    </div>
  );
}
