"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type ReplyResult = { ok: boolean; message: string };

/** Human takeover: the API sends through the MCP gateway as `human_operator` — conduct rules, amount checks,
 * WhatsApp's 24-hour window and the audit chain apply to people exactly as to agents. */
export async function sendReply(_: ReplyResult | null, formData: FormData): Promise<ReplyResult> {
  const customer = String(formData.get("customer_id") ?? "");
  const text = String(formData.get("text") ?? "").trim();
  if (!/^cus_[0-9A-Z]{26}$/.test(customer)) return { ok: false, message: "invalid customer" };
  if (text.length < 2 || text.length > 1000) return { ok: false, message: "write 2–1000 characters" };
  try {
    await api(`/v1/conversations/${customer}/reply`, { method: "POST", body: { text } });
    revalidatePath("/conversations");
    return { ok: true, message: "Sent" };
  } catch (e) {
    const raw = e instanceof Error ? e.message : "failed";
    const reason = raw.match(/"reason":\s*"([^"]+)"/)?.[1];
    return { ok: false, message: reason ?? raw };
  }
}
