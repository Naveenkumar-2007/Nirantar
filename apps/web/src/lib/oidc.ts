import "server-only";

import * as client from "openid-client";

/** OIDC relying party for the dashboard (authorization code + PKCE + nonce, confidential client).
 * Configuration is discovered from the issuer and cached per process. */
let cached: Promise<client.Configuration> | null = null;

export function oidcEnabled(): boolean {
  return Boolean(process.env.NIRANTAR_OIDC_ISSUER);
}

export function appUrl(): string {
  return process.env.NIRANTAR_APP_URL ?? "http://localhost:3010";
}

export function config(): Promise<client.Configuration> {
  if (!cached) {
    const issuer = new URL(process.env.NIRANTAR_OIDC_ISSUER as string);
    const options = issuer.protocol === "http:" ? { execute: [client.allowInsecureRequests] } : undefined;
    cached = client
      .discovery(issuer, process.env.NIRANTAR_OIDC_CLIENT_ID as string, undefined,
        client.ClientSecretPost(process.env.NIRANTAR_OIDC_CLIENT_SECRET), options)
      .catch((e) => {
        cached = null;
        throw e;
      });
  }
  return cached;
}

export { client };
