"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type ActionResult = { ok: boolean; message: string };

const VERSION = /^[0-9]{1,6}$/;

/** Human override: retire a live model version. Needs policy:admin and a reason (audited). */
export async function retireVersion(_: ActionResult | null, fd: FormData): Promise<ActionResult> {
  const version = String(fd.get("version") ?? "");
  const reason = String(fd.get("reason") ?? "").trim();
  if (!VERSION.test(version)) return { ok: false, message: "invalid version" };
  if (reason.length < 3) return { ok: false, message: "Give a reason (kept in the audit trail)." };
  try {
    await api(`/v1/ml/models/${version}/retire`, { method: "POST", body: { reason } });
    revalidatePath("/models");
    return { ok: true, message: `v${version} retired — decisions fall back to the champion or the prior` };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}

export async function trainNow(): Promise<ActionResult> {
  try {
    await api("/v1/ml/train", { method: "POST" });
    revalidatePath("/models");
    return { ok: true, message: "Training started — refresh in a minute" };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}
