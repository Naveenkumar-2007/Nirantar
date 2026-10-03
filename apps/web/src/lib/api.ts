import "server-only";

import { redirect } from "next/navigation";
import { getSession } from "@/lib/auth";

/** Server-side client for the Nirantar API.
 * Signed-in mode (OIDC configured): every call carries the PERSON's access token and the business they act in;
 * the API verifies the token and applies that person's roles. Local key mode (no OIDC): the server-side keys from
 * .env.local. Either way, credentials stay on the server; the browser never sees them. */
const BASE = process.env.NIRANTAR_API_URL ?? "http://localhost:8080";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type KeyKind = "viewer" | "approver" | "platform";

function headers(kind: KeyKind): Record<string, string> {
  if (kind === "platform") return { "X-Platform-Key": process.env.NIRANTAR_PLATFORM_KEY ?? "" };
  const key = kind === "approver" ? process.env.NIRANTAR_APPROVER_KEY : process.env.NIRANTAR_API_KEY;
  return { Authorization: `Bearer ${key ?? ""}` };
}

async function authHeaders(kind: KeyKind): Promise<Record<string, string>> {
  if (kind === "platform") return headers(kind);
  const session = await getSession();
  if (session === null && process.env.NIRANTAR_OIDC_ISSUER) redirect("/login");
  if (session) {
    const h: Record<string, string> = { Authorization: `Bearer ${session.accessToken}` };
    if (session.tenant) h["X-Nirantar-Tenant"] = session.tenant;
    return h;
  }
  return headers(kind);
}

export async function api<T>(path: string, init: { kind?: KeyKind; method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: init.method ?? "GET",
    headers: { ...(await authHeaders(init.kind ?? "viewer")), "Content-Type": "application/json" },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    cache: "no-store",
  });
  if (res.status === 401 && process.env.NIRANTAR_OIDC_ISSUER && (init.kind ?? "viewer") !== "platform") {
    redirect("/login?error=session");
  }
  if (res.status === 409 && !path.startsWith("/v1/me") && !path.startsWith("/v1/businesses")) {
    const d = await res.clone().json().catch(() => ({}));
    if (d?.detail?.code === "no_business") redirect("/onboarding");
    if (d?.detail?.code === "choose_business") redirect("/choose-business");
  }
  if (!res.ok) {
    let detail: string = res.statusText;
    try {
      const d = (await res.json()).detail;
      if (typeof d === "string") detail = d;
      else if (d && Array.isArray(d.problems)) detail = d.problems.join("; ");   // template checks
      else if (Array.isArray(d)) detail = d.map((e: { msg?: string }) => e.msg ?? JSON.stringify(e)).join("; ");
      else if (d) detail = JSON.stringify(d);
    } catch {}
    throw new ApiError(res.status, `${res.status} ${detail}`);
  }
  return (await res.json()) as T;
}
