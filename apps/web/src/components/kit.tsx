import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Card as UiCard, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table as UiTable, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

/** Nirantar's page-level kit (P8.3): built on shadcn/ui primitives and the design tokens in globals.css.
 * Every console page uses these, so the whole product changes look in one place. */

export function PageHeader({ title, subtitle, right, eyebrow }: {
  title: string; subtitle?: string; right?: ReactNode; eyebrow?: string;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {eyebrow && <div className="mb-1 text-xs font-medium uppercase tracking-wider text-primary">{eyebrow}</div>}
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">{title}</h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{subtitle}</p>}
      </div>
      {right && <div className="flex items-center gap-2">{right}</div>}
    </div>
  );
}

export function Card({ title, description, action, children, className = "" }: {
  title?: string; description?: string; action?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <UiCard className={cn("shadow-card", className)}>
      {(title || action) && (
        <CardHeader>
          {title && <CardTitle className="text-sm font-semibold">{title}</CardTitle>}
          {description && <CardDescription>{description}</CardDescription>}
          {action && <CardAction>{action}</CardAction>}
        </CardHeader>
      )}
      <CardContent>{children}</CardContent>
    </UiCard>
  );
}

export function Stat({ label, value, hint, icon, emphasis }: {
  label: string; value: ReactNode; hint?: ReactNode; icon?: ReactNode; emphasis?: boolean;
}) {
  return (
    <UiCard className={cn("shadow-card", emphasis && "ring-primary/30 bg-gradient-to-br from-card to-primary/5")}>
      <CardContent>
        <div className="flex items-center justify-between gap-2 text-xs font-medium text-muted-foreground">
          <span>{label}</span>
          {icon && <span className="text-muted-foreground [&>svg]:size-4">{icon}</span>}
        </div>
        <div className="mt-2 text-2xl font-semibold tracking-tight text-foreground">{value}</div>
        {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </UiCard>
  );
}

const TONE = {
  good: "bg-success-soft text-success",
  bad: "bg-danger-soft text-danger",
  warn: "bg-warning-soft text-warning",
  info: "bg-info-soft text-info",
  neutral: "bg-muted text-muted-foreground",
} as const;
type Tone = keyof typeof TONE;
/* shape cue next to the colour: state is never carried by colour alone */
const MARK: Record<Tone, string> = { good: "●", bad: "■", warn: "▲", info: "◆", neutral: "○" };

export function toneFor(status: string | null | undefined): Tone {
  const s = (status ?? "").toLowerCase();
  if (["succeeded", "executed", "verified", "allow", "granted", "completed", "recovered", "captured", "paid_on_time",
    "ok", "closed", "active", "done", "ready", "won", "delivered", "read"].includes(s)) return "good";
  if (["failed", "deny", "denied", "unrecovered", "dead", "violation", "error", "revoked", "lost", "rejected"]
    .includes(s)) return "bad";
  if (["pending", "pending_approval", "require_approval", "require_more_information", "attempting", "notified", "open",
    "waiting", "escalated", "todo"].includes(s)) return "warn";
  if (["scheduled", "proposed", "working", "running", "sent", "submitted"].includes(s)) return "info";
  return "neutral";
}

export function Badge({ children, tone, mark = true }: { children: ReactNode; tone?: Tone; mark?: boolean }) {
  const t = tone ?? toneFor(String(children));
  return (
    <span className={cn("inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-full px-2 text-xs font-medium",
      TONE[t])}>
      {mark && <span aria-hidden className="text-[0.6rem] leading-none">{MARK[t]}</span>}
      {children}
    </span>
  );
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <UiTable>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          {head.map((h, i) => (
            <TableHead key={`${h}-${i}`} className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {h}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>{children}</TableBody>
    </UiTable>
  );
}

export const Tr = TableRow;

/** Single-line by default (the table scrolls horizontally on small screens); `wrap` for long free text. */
export function Td({ children, className = "", wrap = false }: { children: ReactNode; className?: string; wrap?: boolean }) {
  return <TableCell className={cn("align-top", wrap ? "min-w-48 max-w-md whitespace-normal" : "whitespace-nowrap", className)}>
    {children}</TableCell>;
}

export function Empty({ title, hint, icon }: { title: string; hint?: string; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border px-6 py-10 text-center">
      {icon && <div className="mb-3 text-muted-foreground [&>svg]:size-6">{icon}</div>}
      <div className="text-sm font-medium text-foreground">{title}</div>
      {hint && <div className="mt-1 max-w-sm text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-xs text-foreground">{children}</code>;
}
