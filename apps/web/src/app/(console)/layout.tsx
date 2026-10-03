import Link from "next/link";
import { api } from "@/lib/api";
import { getSession } from "@/lib/auth";
import { switchBusiness } from "@/lib/business-actions";

type Me = { businesses: { tenant_id: string; name: string; roles: string[] }[] };

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/setup", label: "Setup" },
  { href: "/debits", label: "Debits" },
  { href: "/agents", label: "AI agents" },
  { href: "/approvals", label: "Approvals" },
  { href: "/retention", label: "Retention & win-back" },
  { href: "/automations", label: "Automations" },
  { href: "/data", label: "Data & readiness" },
  { href: "/compliance", label: "Compliance" },
  { href: "/experiments", label: "Experiments" },
  { href: "/models", label: "Your models" },
  { href: "/connect/mcp", label: "Connected AI apps" },
  { href: "/audit", label: "Audit" },
  { href: "/assistant", label: "Policy assistant" },
  { href: "/settings/team", label: "Team" },
  { href: "/platform", label: "Platform console" },
];

export default async function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const session = await getSession();
  const me = session ? await api<Me>("/v1/me") : null;
  const current = me?.businesses.find((b) => b.tenant_id === session?.tenant) ?? (me?.businesses.length === 1 ? me.businesses[0] : undefined);
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="border-b border-[var(--border)] bg-[var(--card)] md:flex md:w-56 md:shrink-0 md:flex-col md:border-b-0 md:border-r">
        <div className="px-4 py-4">
          <div className="text-base font-semibold tracking-tight">Nirantar</div>
          <div className="text-xs text-[var(--muted)]">Recurring-revenue OS</div>
          {current && (
            <div className="mt-3 rounded-lg border border-[var(--border)] px-3 py-2">
              <div className="truncate text-sm font-medium">{current.name}</div>
              {me && me.businesses.length > 1 && (
                <details className="mt-1 text-xs">
                  <summary className="cursor-pointer text-[var(--muted)] hover:text-[var(--fg)]">Switch business</summary>
                  <ul className="mt-1 space-y-1">
                    {me.businesses.filter((b) => b.tenant_id !== current.tenant_id).map((b) => (
                      <li key={b.tenant_id}>
                        <form action={switchBusiness}>
                          <input type="hidden" name="tenant_id" value={b.tenant_id} />
                          <button className="w-full truncate text-left text-[var(--muted)] hover:text-[var(--fg)]">{b.name}</button>
                        </form>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          )}
        </div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-3 md:flex-1 md:flex-col md:overflow-visible">
          {NAV.map((n) => (
            <Link key={n.href} href={n.href}
              className="whitespace-nowrap rounded-md px-3 py-1.5 text-sm text-[var(--muted)] hover:bg-[var(--chip)] hover:text-[var(--fg)]">
              {n.label}
            </Link>
          ))}
        </nav>
        {session && (
          <div className="hidden border-t border-[var(--border)] px-4 py-3 md:block">
            <div className="truncate text-sm font-medium">{session.name ?? session.email}</div>
            {session.email && session.name && <div className="truncate text-xs text-[var(--muted)]">{session.email}</div>}
            <form action="/auth/logout" method="post" className="mt-2">
              <button className="text-xs text-[var(--muted)] underline-offset-2 hover:text-[var(--fg)] hover:underline">Sign out</button>
            </form>
          </div>
        )}
      </aside>
      <main className="min-w-0 flex-1 px-4 py-6 md:px-8">{children}</main>
    </div>
  );
}
