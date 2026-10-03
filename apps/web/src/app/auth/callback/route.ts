import { NextResponse, type NextRequest } from "next/server";
import { appUrl, client, config } from "@/lib/oidc";
import { FLOW_COOKIE, REFRESH_COOKIE, SESSION_COOKIE, cookieOptions, seal, unseal, type Flow, type Session } from "@/lib/session";

const MAX_AGE = 60 * 60 * 24;

/** Finish sign-in: verify state, PKCE and the ID token nonce, then store the encrypted session. */
export async function GET(request: NextRequest) {
  const flow = await unseal<Flow>(request.cookies.get(FLOW_COOKIE)?.value);
  if (!flow) return NextResponse.redirect(new URL("/login?error=expired", appUrl()));
  const cfg = await config();
  // the provider redirected to our public URL; rebuild it from that origin (not the dev server's host header)
  const current = new URL(`${appUrl()}/auth/callback${request.nextUrl.search}`);
  let tokens: Awaited<ReturnType<typeof client.authorizationCodeGrant>>;
  try {
    tokens = await client.authorizationCodeGrant(cfg, current, {
      pkceCodeVerifier: flow.verifier, expectedState: flow.state, expectedNonce: flow.nonce, idTokenExpected: true,
    });
  } catch {
    return NextResponse.redirect(new URL("/login?error=signin_failed", appUrl()));
  }
  const claims = tokens.claims();
  if (!claims) return NextResponse.redirect(new URL("/login?error=signin_failed", appUrl()));
  const session: Session = {
    accessToken: tokens.access_token,
    expiresAt: Math.floor(Date.now() / 1000) + (tokens.expires_in ?? 300),
    sub: claims.sub,
    name: typeof claims.name === "string" ? claims.name : undefined,
    email: typeof claims.email === "string" ? claims.email : undefined,
  };
  const res = NextResponse.redirect(new URL(flow.returnTo || "/", appUrl()));
  res.cookies.delete(FLOW_COOKIE);
  res.cookies.set(SESSION_COOKIE, await seal(session, MAX_AGE), cookieOptions(MAX_AGE));
  if (tokens.refresh_token) {
    res.cookies.set(REFRESH_COOKIE, await seal({ refreshToken: tokens.refresh_token }, MAX_AGE), cookieOptions(MAX_AGE));
  }
  return res;
}
