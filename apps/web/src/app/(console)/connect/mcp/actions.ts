"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { api } from "@/lib/api";

export type ConnectResult = { ok: boolean; message: string };

const SCOPES = ["nirantar:read", "nirantar:act", "nirantar:money"];

/** Approve or refuse an MCP client's connection request. The API checks tenant:admin, single use and expiry,
 * then returns the client's redirect URL (with a one-time code, or error=access_denied). */
export async function decideConnection(_: ConnectResult | null, formData: FormData): Promise<ConnectResult> {
  const id = String(formData.get("request_id") ?? "");
  const approve = formData.get("approve") === "true";
  const scopes = formData.getAll("scope").map(String).filter((s) => SCOPES.includes(s));
  if (!/^oar_[A-Za-z0-9_-]{20,64}$/.test(id)) return { ok: false, message: "invalid request id" };
  if (approve && scopes.length === 0) return { ok: false, message: "Choose at least one permission" };
  let target: string;
  try {
    const r = await api<{ redirect_url: string }>(`/v1/mcp/requests/${id}/decide`, {
      method: "POST", body: { approve, scopes: approve ? scopes : null } });
    target = r.redirect_url;
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
  redirect(target);
}

export async function revokeConnection(_: ConnectResult | null, formData: FormData): Promise<ConnectResult> {
  const id = String(formData.get("grant_id") ?? "");
  if (!/^grt_[0-9A-Z]{26}$/.test(id)) return { ok: false, message: "invalid connection id" };
  try {
    await api(`/v1/mcp/grants/${id}`, { method: "DELETE" });
    revalidatePath("/connect/mcp");
    return { ok: true, message: "Disconnected — its tokens stopped working immediately" };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}
