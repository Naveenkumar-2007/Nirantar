"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type RefreshResult = { ok: boolean; message: string };

/** Rebuild lifecycle labels, refit sBG/CLV and re-score active subscribers (policy:admin). */
export async function refreshRetention(): Promise<RefreshResult> {
  try {
    await api("/v1/retention/refresh", { method: "POST" });
    revalidatePath("/retention");
    return { ok: true, message: "Refresh started — reload in a minute" };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}
