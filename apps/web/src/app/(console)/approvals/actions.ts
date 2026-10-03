"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type DecideResult = { ok: boolean; message: string };

/** Maker-checker decision. Runs on the server with the approver's key; the API enforces approvals:decide,
 * token scoping and single use, then executes the approved action through the MCP gateway. */
export async function decideApproval(_: DecideResult | null, formData: FormData): Promise<DecideResult> {
  const id = String(formData.get("approval_id") ?? "");
  const grant = formData.get("grant") === "true";
  if (!/^apr_[0-9A-Z]{26}$/.test(id)) return { ok: false, message: "invalid approval id" };
  try {
    const r = await api<{ status: string; executed?: boolean; action_status?: string }>(
      `/v1/approvals/${id}/decide`, { kind: "approver", method: "POST", body: { grant } });
    revalidatePath("/approvals");
    return { ok: true, message: grant ? `Approved · action ${r.action_status ?? "recorded"}` : "Denied" };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}
