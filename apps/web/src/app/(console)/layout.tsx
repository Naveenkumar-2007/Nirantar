import { cookies } from "next/headers";
import { api } from "@/lib/api";
import { getSession } from "@/lib/auth";
import { switchBusiness } from "@/lib/business-actions";
import { AppSidebar } from "@/components/app-sidebar";
import { CommandMenu } from "@/components/command-menu";
import { Separator } from "@/components/ui/separator";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";

type Me = { businesses: { tenant_id: string; name: string; roles: string[] }[] };

export default async function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const session = await getSession();
  const me = session ? await api<Me>("/v1/me") : null;
  const businesses = me?.businesses ?? [];
  const current = businesses.find((b) => b.tenant_id === session?.tenant) ??
    (businesses.length === 1 ? businesses[0] : undefined);
  const open = (await cookies()).get("sidebar_state")?.value !== "false";
  const demo = process.env.NIRANTAR_DEMO === "1";
  return (
    <SidebarProvider defaultOpen={open}>
      <AppSidebar user={session ? { name: session.name, email: session.email } : null} businesses={businesses}
        current={current} switchAction={switchBusiness} demo={demo} />
      <SidebarInset className="min-w-0">
        <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 rounded-t-xl border-b border-border bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-1 data-[orientation=vertical]:h-4" />
          <span className="truncate text-sm text-muted-foreground">{current?.name ?? "Nirantar"}</span>
          <CommandMenu />
          {demo && (
            <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border px-2.5 py-0.5 text-xs text-muted-foreground"
              title="Synthetic business. Mock payments; no WhatsApp message or call is ever sent. Resets on restart.">
              <span aria-hidden className="size-1.5 rounded-full bg-amber-500" />
              Demo · synthetic data · nothing is sent
            </span>
          )}
        </header>
        <main className="min-w-0 flex-1 px-4 py-6 md:px-8">{children}</main>
      </SidebarInset>
    </SidebarProvider>
  );
}
