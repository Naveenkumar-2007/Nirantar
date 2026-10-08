import Link from "next/link";
import {
  ArrowRight, BadgeCheck, Bot, Building2, CalendarClock, FileText, Fingerprint, Landmark, Languages, LineChart,
  Lock, MessageCircle, Phone, ReceiptIndianRupee, Repeat, Scale, ScrollText, ShieldCheck, ShoppingCart, Stethoscope,
} from "lucide-react";
import { BrandMark, Wordmark } from "@/components/brand";

export const metadata = {
  title: "Nirantar — revenue recovery you can prove",
  description: "Nirantar recovers failed subscription payments, abandoned checkouts, overdue invoices and broken "
    + "payment promises — within policy, on WhatsApp and voice, in Indian languages — and proves every rupee "
    + "against a holdout.",
};

const LEAKS = [
  { icon: Repeat, title: "Failed recurring payments", body: "UPI AutoPay and e-mandate debits that bounce on low balance, bank downtime or revoked mandates." },
  { icon: ShoppingCart, title: "Checkout drop-offs", body: "Carts abandoned on the payment page, UPI timeouts, and payments the bank declined." },
  { icon: ReceiptIndianRupee, title: "Overdue invoices", body: "B2B customers with several unpaid invoices, chased one message at a time." },
  { icon: CalendarClock, title: "Broken promises", body: "\"I'll pay on Friday\" that nobody follows up on Friday." },
];

const STEPS = [
  { icon: Stethoscope, title: "Diagnose", body: "Every failure is classified from the provider's own reason and live bank health. A bank outage is never blamed on the customer." },
  { icon: Scale, title: "Decide within policy", body: "Consent, contact windows, message limits, RBI conduct and TRAI rules are checked before anything is sent. A refusal is final, and logged." },
  { icon: MessageCircle, title: "Act once, well", body: "One message worded for the reason, in the customer's language, with a link for exactly what is owed. Retries only when they can succeed." },
  { icon: BadgeCheck, title: "Verify", body: "Money counts only when the payment provider confirms it. It is booked once, even if a webhook arrives twice." },
  { icon: LineChart, title: "Prove", body: "A randomised holdout is never contacted, so the recovery you see is what Nirantar added — with a confidence interval." },
];

const AGENTS = [
  ["Failure triage", "why a payment failed"], ["Retry sequencer", "when (and whether) to charge again"],
  ["Conversation", "the message, in the customer's language"], ["Voice", "a short call, with a link by WhatsApp"],
  ["Receivables", "one statement, oldest invoice first"], ["Checkout", "one reminder for the right reason"],
  ["Compliance guardian", "checks every action before it runs"], ["Verifier", "trusts the provider, not the agent"],
] as const;

const TRUST = [
  { icon: Lock, title: "Tenant isolation in the database", body: "Every row is protected by Postgres row-level security; the application cannot read another business's data even by mistake." },
  { icon: Fingerprint, title: "Agents cannot go rogue", body: "Agents act only through a gateway with a fixed tool list per agent. They cannot change an amount, skip approval or message without consent." },
  { icon: ScrollText, title: "A tamper-evident audit trail", body: "Every decision — sent, refused, approved, paid — is recorded in a hash-chained log." },
  { icon: ShieldCheck, title: "Built for Indian regulation", body: "RBI fair-practice wording, e-mandate pre-debit notices, TRAI consent and calling rules, DPDP data minimisation." },
];

export default function Welcome() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-20 border-b border-border/60 bg-background/80 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
          <Link href="/welcome" aria-label="Nirantar home"><Wordmark className="text-lg" /></Link>
          <nav aria-label="Primary" className="hidden items-center gap-6 text-sm text-muted-foreground md:flex">
            <a href="#how" className="hover:text-foreground">How it works</a>
            <a href="#proof" className="hover:text-foreground">Proof</a>
            <a href="#trust" className="hover:text-foreground">Security</a>
          </nav>
          <div className="flex items-center gap-2">
            <Link href="/login" className="rounded-md px-3 py-2 text-sm hover:bg-accent">Sign in</Link>
            <Link href="/" className="rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:opacity-90">
              Open the console
            </Link>
          </div>
        </div>
      </header>

      <main>
        {/* hero */}
        <section className="relative overflow-hidden">
          <div aria-hidden className="pointer-events-none absolute inset-x-0 -top-40 mx-auto h-[28rem] max-w-4xl rounded-full bg-primary/10 blur-3xl" />
          <div className="relative mx-auto max-w-6xl px-4 pb-20 pt-20 sm:px-6 sm:pt-28">
            <p className="inline-flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1 text-xs text-muted-foreground">
              <BrandMark className="size-3.5 text-primary" /> निरंतर · continuous
            </p>
            <h1 className="mt-6 max-w-3xl text-4xl font-semibold leading-[1.08] tracking-tight sm:text-6xl">
              Recover the revenue you are losing. <span className="text-muted-foreground">Prove every rupee of it.</span>
            </h1>
            <p className="mt-6 max-w-2xl text-lg leading-relaxed text-muted-foreground">
              Nirantar finds failed payments, abandoned checkouts, overdue invoices and broken promises — diagnoses
              why — and recovers them with one well-timed, compliant message or call in your customer&apos;s language.
              Only money your payment provider confirms is counted, and a holdout shows what Nirantar actually added.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Link href="/" className="inline-flex items-center gap-2 rounded-lg bg-primary px-5 py-3 text-sm font-medium text-primary-foreground hover:opacity-90">
                Explore the live product <ArrowRight className="size-4" aria-hidden />
              </Link>
              <a href="#how" className="rounded-lg border border-border bg-card px-5 py-3 text-sm hover:bg-accent">See how it works</a>
            </div>
            <dl className="mt-14 grid max-w-3xl grid-cols-2 gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-4">
              {[["Channels", "WhatsApp · Voice"], ["Languages", "English · हिंदी · తెలుగు"], ["Payments", "Razorpay · UPI AutoPay"],
                ["Proof", "Holdout + verified"]].map(([k, v]) => (
                <div key={k} className="bg-card px-4 py-3">
                  <dt className="text-xs text-muted-foreground">{k}</dt>
                  <dd className="mt-0.5 text-sm font-medium">{v}</dd>
                </div>
              ))}
            </dl>
          </div>
        </section>

        {/* where revenue leaks */}
        <section aria-labelledby="leaks" className="border-t border-border bg-card/50">
          <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
            <h2 id="leaks" className="text-sm font-medium text-primary">Where revenue leaks</h2>
            <p className="mt-2 max-w-2xl text-3xl font-semibold tracking-tight">Four quiet leaks, one system that closes them.</p>
            <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {LEAKS.map(({ icon: Icon, title, body }) => (
                <div key={title} className="rounded-xl border border-border bg-card p-5 shadow-[var(--shadow-card)]">
                  <Icon className="size-5 text-primary" aria-hidden />
                  <h3 className="mt-4 font-medium">{title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* how it works */}
        <section id="how" aria-labelledby="how-h" className="border-t border-border">
          <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
            <h2 id="how-h" className="text-sm font-medium text-primary">How it works</h2>
            <p className="mt-2 max-w-2xl text-3xl font-semibold tracking-tight">Diagnose, decide, act, verify, prove.</p>
            <ol className="mt-10 grid gap-4 lg:grid-cols-5">
              {STEPS.map(({ icon: Icon, title, body }, i) => (
                <li key={title} className="relative rounded-xl border border-border bg-card p-5">
                  <span className="text-xs tabular-nums text-muted-foreground">0{i + 1}</span>
                  <Icon className="mt-3 size-5 text-primary" aria-hidden />
                  <h3 className="mt-3 font-medium">{title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{body}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        {/* example */}
        <section aria-labelledby="example" className="border-t border-border bg-card/50">
          <div className="mx-auto grid max-w-6xl gap-10 px-4 py-20 sm:px-6 lg:grid-cols-2">
            <div>
              <h2 id="example" className="text-sm font-medium text-primary">A recovery, step by step</h2>
              <p className="mt-2 text-3xl font-semibold tracking-tight">A UPI payment fails at 11:40 pm. Nobody is woken up.</p>
              <p className="mt-4 leading-relaxed text-muted-foreground">
                An illustration of the path every checkout takes in Nirantar. The same steps, the same rules and the
                same verification run for subscriptions, invoices and promises.
              </p>
            </div>
            <ol className="space-y-3" aria-label="Example timeline">
              {[
                ["23:40", "Payment of ₹2,499 fails: the customer's bank had a technical error."],
                ["23:40", "Diagnosed as a bank issue — not the customer's fault, so the wording says so."],
                ["00:10", "The customer has gone quiet. A reminder is due, but it is night: policy holds it."],
                ["09:00", "One WhatsApp message, in Hindi, with a secure link for exactly ₹2,499. No discount."],
                ["09:14", "Paid through the link. The provider confirms the capture; it is booked once."],
                ["—", "Counted as recovered only because a holdout of similar checkouts shows what happens without Nirantar."],
              ].map(([t, s]) => (
                <li key={s} className="flex gap-4 rounded-lg border border-border bg-card p-4">
                  <span className="w-12 shrink-0 font-mono text-sm tabular-nums text-muted-foreground">{t}</span>
                  <span className="text-sm leading-relaxed">{s}</span>
                </li>
              ))}
            </ol>
          </div>
        </section>

        {/* channels + agents */}
        <section aria-labelledby="agents" className="border-t border-border">
          <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
            <div className="grid gap-10 lg:grid-cols-3">
              <div>
                <h2 id="agents" className="text-sm font-medium text-primary">Specialist AI agents</h2>
                <p className="mt-2 text-3xl font-semibold tracking-tight">Each agent does one job, with one set of tools.</p>
                <p className="mt-4 leading-relaxed text-muted-foreground">
                  Durable workflows supervise them. Every action passes the same gate — scope, schema, policy,
                  approval — and is audited. Agents never see a password, choose an amount, or skip a rule.
                </p>
                <ul className="mt-6 space-y-2 text-sm">
                  <li className="flex items-center gap-2"><MessageCircle className="size-4 text-primary" aria-hidden /> WhatsApp with approved templates</li>
                  <li className="flex items-center gap-2"><Phone className="size-4 text-primary" aria-hidden /> Voice calls in Indian languages</li>
                  <li className="flex items-center gap-2"><Languages className="size-4 text-primary" aria-hidden /> English, Hindi and Telugu, more coming</li>
                </ul>
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:col-span-2">
                {AGENTS.map(([name, job]) => (
                  <div key={name} className="flex items-start gap-3 rounded-xl border border-border bg-card p-4">
                    <Bot className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
                    <div><div className="text-sm font-medium">{name}</div><div className="text-sm text-muted-foreground">{job}</div></div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* proof */}
        <section id="proof" aria-labelledby="proof-h" className="border-t border-border bg-card/50">
          <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
            <h2 id="proof-h" className="text-sm font-medium text-primary">Measured, not claimed</h2>
            <p className="mt-2 max-w-3xl text-3xl font-semibold tracking-tight">
              &ldquo;Recovered&rdquo; means your provider confirmed it — and a holdout says Nirantar caused it.
            </p>
            <div className="mt-10 grid gap-4 md:grid-cols-3">
              {[
                [Landmark, "Provider-verified", "Every payment is re-fetched from the provider before it counts. A forged or replayed callback can never mark anything paid."],
                [LineChart, "Against a holdout", "A small random share of customers is never contacted. Recovery is reported as the difference, with a 95% confidence interval."],
                [FileText, "Every step explained", "For each recovered rupee: what failed, why, what was sent, which rule allowed it, and the provider's confirmation."],
              ].map(([Icon, title, body]) => {
                const I = Icon as typeof Landmark;
                return (
                  <div key={title as string} className="rounded-xl border border-border bg-card p-6">
                    <I className="size-5 text-primary" aria-hidden />
                    <h3 className="mt-4 font-medium">{title as string}</h3>
                    <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{body as string}</p>
                  </div>
                );
              })}
            </div>
          </div>
        </section>

        {/* security + compliance */}
        <section id="trust" aria-labelledby="trust-h" className="border-t border-border">
          <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
            <h2 id="trust-h" className="text-sm font-medium text-primary">Security & compliance</h2>
            <p className="mt-2 max-w-2xl text-3xl font-semibold tracking-tight">Safe by construction, not by promise.</p>
            <div className="mt-10 grid gap-4 sm:grid-cols-2">
              {TRUST.map(({ icon: Icon, title, body }) => (
                <div key={title} className="flex gap-4 rounded-xl border border-border bg-card p-5">
                  <Icon className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden />
                  <div><h3 className="font-medium">{title}</h3><p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{body}</p></div>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* CTA */}
        <section className="border-t border-border">
          <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
            <div className="flex flex-col items-start justify-between gap-6 rounded-2xl border border-border bg-card p-8 sm:flex-row sm:items-center sm:p-10">
              <div>
                <p className="text-2xl font-semibold tracking-tight">See your own leakage in a day.</p>
                <p className="mt-2 text-muted-foreground">Connect Razorpay in test mode, import customers, and watch the first recoveries — verified.</p>
              </div>
              <div className="flex flex-wrap gap-3">
                <Link href="/login" className="inline-flex items-center gap-2 rounded-lg bg-primary px-5 py-3 text-sm font-medium text-primary-foreground hover:opacity-90">
                  Get started <ArrowRight className="size-4" aria-hidden />
                </Link>
                <Link href="/" className="rounded-lg border border-border px-5 py-3 text-sm hover:bg-accent">Open the console</Link>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-4 px-4 py-8 text-sm text-muted-foreground sm:flex-row sm:items-center sm:px-6">
          <Wordmark className="text-foreground" />
          <p className="flex items-center gap-2"><Building2 className="size-4" aria-hidden /> Built in India for businesses everywhere.</p>
        </div>
      </footer>
    </div>
  );
}
