"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTheme } from "next-themes";
import {
  Activity, BadgeCheck, Blocks, Bot, Building2, ChevronsUpDown, ClipboardCheck, Contact, Database, FlaskConical, Gauge,
  HandCoins, HeartHandshake, LifeBuoy, LogOut, MessagesSquare, Monitor, Moon, PlugZap, Repeat, Rocket, ScrollText, ShieldCheck, Sparkles,
  Sun, Users, Wallet, Workflow,
} from "lucide-react";
import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader,
  SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarRail,
} from "@/components/ui/sidebar";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";

type Business = { tenant_id: string; name: string; roles: string[] };
type NavItem = { href: string; label: string; icon: React.ComponentType<{ className?: string }> };

const NAV: { label: string; items: NavItem[] }[] = [
  { label: "Operate", items: [
    { href: "/", label: "Overview", icon: Gauge },
    { href: "/recovery", label: "Recovery", icon: HandCoins },
    { href: "/customers", label: "Customers", icon: Contact },
    { href: "/conversations", label: "Conversations", icon: MessagesSquare },
    { href: "/debits", label: "Debits", icon: Wallet },
    { href: "/approvals", label: "Approvals", icon: ClipboardCheck },
    { href: "/agents", label: "AI agents", icon: Bot },
  ] },
  { label: "Grow", items: [
    { href: "/retention", label: "Retention & win-back", icon: HeartHandshake },
    { href: "/experiments", label: "Experiments", icon: FlaskConical },
    { href: "/automations", label: "Automations", icon: Workflow },
  ] },
  { label: "Intelligence", items: [
    { href: "/models", label: "Your models", icon: Sparkles },
    { href: "/data", label: "Data & readiness", icon: Database },
    { href: "/assistant", label: "Policy assistant", icon: LifeBuoy },
  ] },
  { label: "Trust", items: [
    { href: "/compliance", label: "Compliance", icon: ShieldCheck },
    { href: "/audit", label: "Audit trail", icon: ScrollText },
    { href: "/connect/mcp", label: "Connected AI apps", icon: PlugZap },
  ] },
  { label: "Admin", items: [
    { href: "/setup", label: "Setup", icon: Rocket },
    { href: "/settings/team", label: "Team", icon: Users },
    { href: "/platform", label: "Platform console", icon: Blocks },
  ] },
];

function initials(name?: string | null): string {
  return (name ?? "?").split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]?.toUpperCase()).join("") || "?";
}

export function AppSidebar({ user, businesses, current, switchAction }: {
  user: { name?: string; email?: string } | null;
  businesses: Business[];
  current?: Business;
  switchAction: (formData: FormData) => Promise<void>;
}) {
  const pathname = usePathname();
  const { theme, setTheme } = useTheme();
  const isActive = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));
  return (
    <Sidebar collapsible="icon" variant="inset">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton size="lg" className="data-[state=open]:bg-sidebar-accent">
                  <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                    <Repeat className="size-4" />
                  </div>
                  <div className="grid flex-1 text-left leading-tight">
                    <span className="truncate text-sm font-semibold">{current?.name ?? "Nirantar"}</span>
                    <span className="truncate text-xs text-muted-foreground">
                      {current ? current.roles.join(", ").replaceAll("_", " ") : "Recurring-revenue OS"}
                    </span>
                  </div>
                  {businesses.length > 0 && <ChevronsUpDown className="ml-auto size-4 text-muted-foreground" />}
                </SidebarMenuButton>
              </DropdownMenuTrigger>
              {businesses.length > 0 && (
                <DropdownMenuContent className="min-w-60" align="start" side="bottom">
                  <DropdownMenuLabel className="text-xs text-muted-foreground">Businesses</DropdownMenuLabel>
                  {businesses.map((b) => (
                    <form key={b.tenant_id} action={switchAction}>
                      <input type="hidden" name="tenant_id" value={b.tenant_id} />
                      <input type="hidden" name="return_to" value="/" />
                      <DropdownMenuItem asChild>
                        <button className="w-full cursor-pointer">
                          <Building2 className="size-4" />
                          <span className="truncate">{b.name}</span>
                          {b.tenant_id === current?.tenant_id && <BadgeCheck className="ml-auto size-4 text-primary" />}
                        </button>
                      </DropdownMenuItem>
                    </form>
                  ))}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem asChild>
                    <Link href="/onboarding?new=1"><Rocket className="size-4" />Start another business</Link>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              )}
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        {NAV.map((group) => (
          <SidebarGroup key={group.label}>
            <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {group.items.map((item) => (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton asChild isActive={isActive(item.href)} tooltip={item.label}>
                      <Link href={item.href}><item.icon /><span>{item.label}</span></Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton size="lg" className="data-[state=open]:bg-sidebar-accent">
                  <Avatar className="size-8 rounded-lg">
                    <AvatarFallback className="rounded-lg bg-primary/10 text-xs font-semibold text-primary">
                      {initials(user?.name ?? user?.email)}
                    </AvatarFallback>
                  </Avatar>
                  <div className="grid flex-1 text-left leading-tight">
                    <span className="truncate text-sm font-medium">{user?.name ?? "Local mode"}</span>
                    <span className="truncate text-xs text-muted-foreground">{user?.email ?? "No sign-in configured"}</span>
                  </div>
                  <ChevronsUpDown className="ml-auto size-4 text-muted-foreground" />
                </SidebarMenuButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent className="min-w-56" side="top" align="end">
                <DropdownMenuLabel className="text-xs text-muted-foreground">Appearance</DropdownMenuLabel>
                <DropdownMenuRadioGroup value={theme ?? "system"} onValueChange={setTheme}>
                  <DropdownMenuRadioItem value="light"><Sun className="size-4" />Light</DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="dark"><Moon className="size-4" />Dark</DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="system"><Monitor className="size-4" />System</DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
                {user && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuGroup>
                      <DropdownMenuItem asChild>
                        <Link href="/settings/team"><Users className="size-4" />Team</Link>
                      </DropdownMenuItem>
                      <DropdownMenuItem asChild>
                        <Link href="/audit"><Activity className="size-4" />Activity</Link>
                      </DropdownMenuItem>
                    </DropdownMenuGroup>
                    <DropdownMenuSeparator />
                    <form action="/auth/logout" method="post">
                      <DropdownMenuItem asChild>
                        <button className="w-full cursor-pointer"><LogOut className="size-4" />Sign out</button>
                      </DropdownMenuItem>
                    </form>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}

