import { redirect } from "next/navigation";
import { api } from "@/lib/api";
import { switchBusiness } from "@/lib/business-actions";

type Me = { name: string | null; businesses: { tenant_id: string; name: string; roles: string[] }[] };

export default async function ChooseBusinessPage() {
  const me = await api<Me>("/v1/me");
  if (me.businesses.length === 0) redirect("/onboarding");
  return (
    <div className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-md">
        <h1 className="mb-1 text-2xl font-semibold tracking-tight">Choose a business</h1>
        <p className="mb-6 text-sm text-muted-foreground">You belong to more than one. You can switch any time from the sidebar.</p>
        <ul className="space-y-2">
          {me.businesses.map((b) => (
            <li key={b.tenant_id}>
              <form action={switchBusiness}>
                <input type="hidden" name="tenant_id" value={b.tenant_id} />
                <button className="flex w-full items-center justify-between rounded-xl border border-border bg-card px-4 py-3 text-left hover:border-primary">
                  <span className="font-medium">{b.name}</span>
                  <span className="text-xs text-muted-foreground">{b.roles.join(", ").replaceAll("_", " ")}</span>
                </button>
              </form>
            </li>
          ))}
        </ul>
        <a href="/onboarding?new=1" className="mt-4 block text-center text-sm text-muted-foreground hover:text-foreground">+ Start another business</a>
      </div>
    </div>
  );
}
