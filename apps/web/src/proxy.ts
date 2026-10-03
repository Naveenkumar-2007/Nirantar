import { NextResponse, type NextRequest } from "next/server";
import { config as oidcConfig, client, oidcEnabled } from "@/lib/oidc";
import { REFRESH_COOKIE, SESSION_COOKIE, cookieOptions, seal, unseal, type Session } from "@/lib/session";

/** Optimistic auth gate (Next.js Proxy): no session → /login. Near-expiry access token → refresh it here, the one
 * place that can set cookies before Server Components render. Authorization itself is enforced by the API on every
 * call (the token is verified there); this only decides where the browser goes. */
const PUBLIC = ["/login", "/auth/", "/favicon.ico", "/_next/"];
const REFRESH_MAX_AGE = 60 * 60 * 24;

export async function proxy(request: NextRequest) {
  if (!oidcEnabled()) return NextResponse.next();
  const { pathname, search } = request.nextUrl;
  if (PUBLIC.some((p) => pathname.startsWith(p))) return NextResponse.next();

  const session = await unseal<Session>(request.cookies.get(SESSION_COOKIE)?.value);
  const toLogin = () => {
    const url = new URL("/login", request.url);
    url.searchParams.set("returnTo", pathname + search);
    const res = NextResponse.redirect(url);
    res.cookies.delete(SESSION_COOKIE);
    res.cookies.delete(REFRESH_COOKIE);
    return res;
  };
  if (!session) return toLogin();
  if (session.expiresAt - 30 > Date.now() / 1000) return NextResponse.next();

  const refresh = await unseal<{ refreshToken: string }>(request.cookies.get(REFRESH_COOKIE)?.value);
  if (!refresh) return toLogin();
  try {
    const tokens = await client.refreshTokenGrant(await oidcConfig(), refresh.refreshToken);
    const next: Session = { ...session, accessToken: tokens.access_token,
      expiresAt: Math.floor(Date.now() / 1000) + (tokens.expires_in ?? 300) };
    const sealed = await seal(next, REFRESH_MAX_AGE);
    request.cookies.set(SESSION_COOKIE, sealed);              // this request renders with the fresh token
    const res = NextResponse.next({ request });
    res.cookies.set(SESSION_COOKIE, sealed, cookieOptions(REFRESH_MAX_AGE));
    if (tokens.refresh_token) {                               // rotation: Keycloak issues a new refresh token
      res.cookies.set(REFRESH_COOKIE, await seal({ refreshToken: tokens.refresh_token }, REFRESH_MAX_AGE),
        cookieOptions(REFRESH_MAX_AGE));
    }
    return res;
  } catch {
    return toLogin();
  }
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
