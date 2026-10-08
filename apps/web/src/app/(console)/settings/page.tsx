import Link from "next/link";
import {
  Bot, Building2, KeyRound, MessageCircle, PlugZap, Scale, ScrollText, ShieldCheck, SlidersHorizontal, Users, Webhook,
} from "lucide-react";
import { api } from "@/lib/api";
import { getSession } from "@/lib/auth";
import { Card, Mono, PageHeader } from "@/components/kit";

type Me = { businesses: { tenant_id: string; name: string; roles: string[] }[] };

const SECTIONS = [
  { href: "/settings/team", icon: Users, title: "Team & roles", body: "Invite people and choose what each can do: owner, operator, approver, compliance, viewer." },
  { href: "/automations", icon: SlidersHorizontal, title: "Recovery rules", body: "Contact windows, message limits, retry and reminder timing, holdout size, templates in each language." },
  { href: "/setup", icon: Building2, title: "Business & payments", body: "Your business profile and the payment provider Nirantar collects through (Razorpay test or live)." },
  { href: "/connect/mcp", icon: PlugZap, title: "Integrations & API", body: "API keys, a store key for checkout events, and AI apps connected over MCP — each with its own permissions." },
  { href: "/compliance", icon: Scale, title: "Compliance", body: "The policies every action is checked against, and what each one blocked." },
  { href: "/agents", icon: Bot, title: "AI agents", body: "What each agent may do, what it did, and every decision with its policy result." },
  { href: "/audit", icon: ScrollText, title: "Audit trail", body: "The tamper-evident record of every action, approval and change." },
] as const;

export const metadata = { title: "Settings" };

export default async function SettingsPage() {
  const [session, me] = await Promise.all([getSession(), api<Me>("/v1/me").catch(() => null)]);
  const businesses = me?.businesses ?? [];
  const tenant = businesses.find((b) => b.tenant_id === session?.tenant)?.tenant_id ?? businesses[0]?.tenant_id;
  const base = (process.env.NIRANTAR_APP_URL ?? "https://app.your-domain.example").replace(/\/$/, "");
  const hooks: [string, string, string][] = [
    ["Razorpay", `${base}/webhooks/razorpay/${tenant ?? "<your-business-id>"}`,
      "Dashboard → Settings → Webhooks. Events: payment.*, order.paid, refund.*, payment.dispute.*, token.*. Use your webhook secret."],
    ["WhatsApp (Meta)", `${base}/webhooks/whatsapp`, "Meta app → WhatsApp → Configuration. Subscribe to messages. Verify token from your settings."],
    ["Exotel status callback", `${base}/webhooks/exotel/${tenant ?? "<your-business-id>"}`, "Set as the call status callback on the Voicebot flow."],
    ["Exotel Voicebot stream", `${base.replace(/^http/, "ws")}/voice/exotel`, "WebSocket URL in the Voicebot applet. Calls carry a signed token; anything else is refused."],
  ];
  return (
    <>
      <PageHeader eyebrow="Admin" title="Settings" subtitle="Everything that shapes how Nirantar works for your business, in one place." />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {SECTIONS.map(({ href, icon: Icon, title, body }) => (
          <Link key={href} href={href}
            className="group rounded-xl border border-border bg-card p-5 shadow-[var(--shadow-card)] outline-none transition-colors hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring">
            <Icon className="size-5 text-primary" aria-hidden />
            <div className="mt-3 font-medium">{title}</div>
            <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{body}</p>
          </Link>
        ))}
      </div>

      <Card title="Security" description="Sign-in is handled by Nirantar's identity service; your password never reaches the app." className="mt-6">
        <div className="flex flex-wrap gap-3">
          <a href="/auth/login?action=mfa&returnTo=/settings" className="inline-flex items-center gap-2 rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:opacity-90">
            <ShieldCheck className="size-4" aria-hidden /> Turn on two-step verification
          </a>
          <a href="/auth/login?action=password&returnTo=/settings" className="inline-flex items-center gap-2 rounded-md border border-border px-3.5 py-2 text-sm hover:bg-accent">
            <KeyRound className="size-4" aria-hidden /> Change password
          </a>
          <a href="/auth/logout" className="inline-flex items-center gap-2 rounded-md border border-border px-3.5 py-2 text-sm hover:bg-accent">
            Sign out everywhere on this device
          </a>
        </div>
        <ul className="mt-4 space-y-1.5 text-sm text-muted-foreground">
          <li>Sessions are encrypted, http-only cookies; access tokens last 10 minutes and are refreshed with rotation.</li>
          <li>Repeated wrong passwords lock the account temporarily (brute-force protection).</li>
          <li>Two-step verification uses any authenticator app (Google Authenticator, Microsoft Authenticator, FreeOTP).</li>
        </ul>
      </Card>

      <Card title="Webhooks" description="Paste these into each provider. Every delivery is signature-checked and recorded once; replays change nothing." className="mt-6">
        <ul className="divide-y divide-border">
          {hooks.map(([name, url, how]) => (
            <li key={name} className="flex flex-col gap-1 py-3 first:pt-0 last:pb-0">
              <div className="flex items-center gap-2 text-sm font-medium">
                {name.startsWith("WhatsApp") ? <MessageCircle className="size-4 text-primary" aria-hidden /> : <Webhook className="size-4 text-primary" aria-hidden />}
                {name}
              </div>
              <Mono>{url}</Mono>
              <p className="text-xs text-muted-foreground">{how}</p>
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}
