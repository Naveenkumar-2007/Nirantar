"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type ConnectResult = {
  ok: boolean;
  message: string;
  webhook?: { url: string; secret: string; events: string[]; mode: string };
};

/** Connect the business's own Razorpay account. The API verifies the keys with Razorpay, then stores them
 * encrypted; they never come back. The webhook secret is returned once, for the merchant to paste into Razorpay. */
export async function connectRazorpay(_: ConnectResult | null, formData: FormData): Promise<ConnectResult> {
  const keyId = String(formData.get("key_id") ?? "").trim();
  const keySecret = String(formData.get("key_secret") ?? "").trim();
  if (!/^rzp_(test|live)_[A-Za-z0-9]{8,32}$/.test(keyId)) {
    return { ok: false, message: "Key id looks like rzp_test_… (from Razorpay → Account & Settings → API keys)" };
  }
  if (keySecret.length < 12) return { ok: false, message: "Enter the key secret shown when you generated the key" };
  try {
    const r = await api<{ mode: string; webhook_url: string; webhook_secret: string; webhook_events: string[] }>(
      "/v1/onboarding/razorpay", { method: "POST", body: { key_id: keyId, key_secret: keySecret } });
    revalidatePath("/setup");
    return { ok: true, message: `Connected in ${r.mode} mode`,
      webhook: { url: r.webhook_url, secret: r.webhook_secret, events: r.webhook_events, mode: r.mode } };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message.replace(/^\d+ /, "") : "failed" };
  }
}

export type ImportResult = { ok: boolean; message: string };

export async function startImport(): Promise<ImportResult> {
  try {
    await api("/v1/onboarding/import", { method: "POST" });
    revalidatePath("/setup");
    return { ok: true, message: "Import started" };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message.replace(/^\d+ /, "") : "failed" };
  }
}
