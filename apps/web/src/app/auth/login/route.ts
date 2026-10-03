import { NextResponse, type NextRequest } from "next/server";
import { appUrl, client, config } from "@/lib/oidc";
import { FLOW_COOKIE, cookieOptions, seal } from "@/lib/session";

/** Start sign-in: authorization code + PKCE (S256) + state + nonce, kept in a short-lived encrypted cookie. */
export async function GET(request: NextRequest) {
  const cfg = await config();
  const verifier = client.randomPKCECodeVerifier();
  const state = client.randomState();
  const nonce = client.randomNonce();
  const raw = request.nextUrl.searchParams.get("returnTo") ?? "/";
  const returnTo = raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";   // never an open redirect
  const params: Record<string, string> = {
    redirect_uri: `${appUrl()}/auth/callback`,
    scope: "openid email profile",
    code_challenge: await client.calculatePKCECodeChallenge(verifier),
    code_challenge_method: "S256",
    state,
    nonce,
  };
  if (request.nextUrl.searchParams.get("signup") === "1") params.prompt = "create";   // Keycloak registration page
  const res = NextResponse.redirect(client.buildAuthorizationUrl(cfg, params));
  res.cookies.set(FLOW_COOKIE, await seal({ state, nonce, verifier, returnTo }, 600), cookieOptions(600));
  return res;
}
