import { redirect } from "next/navigation";
import { api } from "@/lib/api";
import { BusinessForm } from "./form";

type Me = { sub: string; email: string | null; name: string | null; businesses: { tenant_id: string; name: string }[] };

export default async function OnboardingPage({ searchParams }: { searchParams: Promise<{ new?: string }> }) {
  const me = await api<Me>("/v1/me");
  if (me.businesses.length > 0 && (await searchParams).new !== "1") redirect("/");
  return (
    <div className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-6">
          <div className="text-sm text-muted-foreground">{me.name ? `Welcome, ${me.name.split(" ")[0]}` : "Welcome"}</div>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">Set up your business</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Next you&apos;ll connect your payment provider and WhatsApp. Nirantar learns from your own history — nothing
            is guessed.
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card p-6 shadow-sm">
          <BusinessForm />
        </div>
      </div>
    </div>
  );
}
