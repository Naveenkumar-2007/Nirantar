import "server-only";

import { EncryptJWT, jwtDecrypt, base64url } from "jose";

/** Encrypted, httpOnly session cookies (JWE dir/A256GCM, key from NIRANTAR_SESSION_SECRET).
 * Two cookies keep each under the 4 KB browser limit: `nir_s` (access token + who/which business) and
 * `nir_r` (refresh token). Tokens never reach browser JavaScript. */
export const SESSION_COOKIE = "nir_s";
export const REFRESH_COOKIE = "nir_r";
export const FLOW_COOKIE = "nir_flow";

export type Session = {
  accessToken: string;
  expiresAt: number;            // epoch seconds
  sub: string;
  name?: string;
  email?: string;
  tenant?: string;              // the business the user is acting in
  idToken?: string;
};

export type Flow = { state: string; nonce: string; verifier: string; returnTo: string };

function key(): Uint8Array {
  const secret = process.env.NIRANTAR_SESSION_SECRET;
  if (!secret) throw new Error("NIRANTAR_SESSION_SECRET is not set");
  const k = base64url.decode(secret);
  if (k.length !== 32) throw new Error("NIRANTAR_SESSION_SECRET must be 32 bytes (base64url)");
  return k;
}

export async function seal(payload: Record<string, unknown>, maxAgeS: number): Promise<string> {
  return new EncryptJWT(payload)
    .setProtectedHeader({ alg: "dir", enc: "A256GCM" })
    .setIssuedAt()
    .setExpirationTime(`${maxAgeS}s`)
    .encrypt(key());
}

export async function unseal<T>(value: string | undefined): Promise<T | null> {
  if (!value) return null;
  try {
    const { payload } = await jwtDecrypt(value, key());
    return payload as unknown as T;
  } catch {
    return null;
  }
}

export const cookieOptions = (maxAgeS: number) => ({
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  maxAge: maxAgeS,
});
