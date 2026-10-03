"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type SyncResult = { ok: boolean; message: string };

/** Start a pipeline run (backfill → changes → silver → gold → health). Needs integrations:admin. */
export async function syncNow(): Promise<SyncResult> {
  try {
    const r = await api<{ run_id: string }>("/v1/data/sync", { method: "POST" });
    revalidatePath("/data");
    return { ok: true, message: `Run ${r.run_id} started — refresh to see progress` };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}
