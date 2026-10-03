import { NextResponse, type NextRequest } from "next/server";
import { appUrl, client, config } from "@/lib/oidc";
import { REFRESH_COOKIE, SESSION_COOKIE, unseal } from "@/lib/session";

/** Sign out: revoke the refresh token at the provider (best effort), clear cookies, end the provider session. */
export async function POST(request: NextRequest) {
  const cfg = await config();
  const refresh = await unseal<{ refreshToken: string }>(request.cookies.get(REFRESH_COOKIE)?.value);
  if (refresh) {
    try {
      await client.tokenRevocation(cfg, refresh.refreshToken);
    } catch {
      /* the session is cleared locally either way */
    }
  }
  const end = client.buildEndSessionUrl(cfg, {
    post_logout_redirect_uri: `${appUrl()}/login`, client_id: process.env.NIRANTAR_OIDC_CLIENT_ID ?? "",
  });
  const res = NextResponse.redirect(end, { status: 303 });
  res.cookies.delete(SESSION_COOKIE);
  res.cookies.delete(REFRESH_COOKIE);
  return res;
}
