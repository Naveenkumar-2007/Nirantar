import type { ReactNode } from "react";

export function PageHeader({ title, subtitle, right }: { title: string; subtitle?: string; right?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-[var(--fg)]">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-[var(--muted)]">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}

export function Card({ title, children, className = "" }: { title?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-[var(--border)] bg-[var(--card)] p-4 ${className}`}>
      {title && <h2 className="mb-3 text-sm font-medium text-[var(--muted)]">{title}</h2>}
      {children}
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <Card>
      <div className="text-xs uppercase tracking-wide text-[var(--muted)]">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-[var(--fg)]">{value}</div>
      {hint && <div className="mt-1 text-xs text-[var(--muted)]">{hint}</div>}
    </Card>
  );
}

const TONE: Record<string, string> = {
  good: "bg-[var(--good-bg)] text-[var(--good-fg)]",
  bad: "bg-[var(--bad-bg)] text-[var(--bad-fg)]",
  warn: "bg-[var(--warn-bg)] text-[var(--warn-fg)]",
  info: "bg-[var(--info-bg)] text-[var(--info-fg)]",
  neutral: "bg-[var(--chip)] text-[var(--muted)]",
};

export function toneFor(status: string | null | undefined): keyof typeof TONE {
  const s = (status ?? "").toLowerCase();
  if (["succeeded", "executed", "verified", "allow", "granted", "completed", "recovered", "captured", "paid_on_time", "ok", "closed"].includes(s)) return "good";
  if (["failed", "deny", "denied", "unrecovered", "dead", "violation", "error"].includes(s)) return "bad";
  if (["pending", "pending_approval", "require_approval", "require_more_information", "attempting", "notified", "open", "waiting"].includes(s)) return "warn";
  if (["scheduled", "proposed", "working"].includes(s)) return "info";
  return "neutral";
}

export function Badge({ children, tone }: { children: ReactNode; tone?: keyof typeof TONE }) {
  const t = tone ?? toneFor(String(children));
  return <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${TONE[t]}`}>{children}</span>;
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wide text-[var(--muted)]">
            {head.map((h) => (
              <th key={h} className="px-3 py-2 font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--border)]">{children}</tbody>
      </table>
    </div>
  );
}

export function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={`px-3 py-2 align-top text-[var(--fg)] ${className}`}>{children}</td>;
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--border)] p-8 text-center">
      <div className="text-sm font-medium text-[var(--fg)]">{title}</div>
      {hint && <div className="mt-1 text-xs text-[var(--muted)]">{hint}</div>}
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="rounded bg-[var(--chip)] px-1 py-0.5 font-mono text-xs text-[var(--fg)]">{children}</code>;
}
