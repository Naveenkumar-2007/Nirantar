import "server-only";

/** Calls to the API's PUBLIC endpoints (customer pay page). No credentials are attached: the signed token in the
 * path is the only key, and the API returns only what a customer may see. */
const BASE = process.env.NIRANTAR_API_URL ?? "http://localhost:8080";

export class PublicApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function publicApi<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new PublicApiError(res.status, typeof d?.detail === "string" ? d.detail : res.statusText);
  }
  return res.json() as Promise<T>;
}
