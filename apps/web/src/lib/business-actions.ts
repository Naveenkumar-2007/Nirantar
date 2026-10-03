"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { api } from "@/lib/api";
import { SESSION_COOKIE, cookieOptions, seal, unseal, type Session } from "@/lib/session";

const MAX_AGE = 60 * 60 * 24;

/** Act in another business. Membership is re-checked with the API (the cookie is never trusted for that). */
export async function switchBusiness(formData: FormData): Promise<void> {
  const tenant = String(formData.get("tenant_id") ?? "");
  const me = await api<{ businesses: { tenant_id: string }[] }>("/v1/me");
  if (!me.businesses.some((b) => b.tenant_id === tenant)) redirect("/choose-business");
  const jar = await cookies();
  const session = await unseal<Session>(jar.get(SESSION_COOKIE)?.value);
  if (!session) redirect("/login");
  jar.set(SESSION_COOKIE, await seal({ ...session, tenant }, MAX_AGE), cookieOptions(MAX_AGE));
  redirect(String(formData.get("return_to") ?? "/").startsWith("/") ? String(formData.get("return_to") ?? "/") : "/");
}
