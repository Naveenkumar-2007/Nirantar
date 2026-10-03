import "server-only";

import { cookies } from "next/headers";
import { oidcEnabled } from "@/lib/oidc";
import { SESSION_COOKIE, unseal, type Session } from "@/lib/session";

/** The signed-in person (null when sign-in is not configured: local key mode). */
export async function getSession(): Promise<Session | null> {
  if (!oidcEnabled()) return null;
  return unseal<Session>((await cookies()).get(SESSION_COOKIE)?.value);
}
