"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type Check = { tool: string; outcome: string; messages: string[]; policy_ids: string[]; retry_after?: string | null };
export type PlanItem = {
  item_id: string; kind: string; display_name: string | null; amount_minor: number; action: string;
  message: string | null; template_ref: string | null; decision: string; checks: Check[];
};
export type Plan = { items: PlanItem[]; counts: Record<string, number>; previewed_at: string };
export type Result<T> = { ok: true; data: T } | { ok: false; message: string };

const ITEM = /^(failed_debit|at_risk_debit|mandate):[A-Za-z0-9_]+$/;

function clean(ids: string[]): string[] | null {
  const out = [...new Set(ids)];
  return out.length > 0 && out.length <= 500 && out.every((i) => ITEM.test(i)) ? out : null;
}

function fail(e: unknown): { ok: false; message: string } {
  return { ok: false, message: e instanceof Error ? e.message : "failed" };
}

/** Dry run on the server: the exact message per customer and the Compliance Guardian's decision. Sends nothing. */
export async function planBatch(itemIds: string[]): Promise<Result<Plan>> {
  const ids = clean(itemIds);
  if (!ids) return { ok: false, message: "select between 1 and 500 items" };
  try {
    return { ok: true, data: await api<Plan>("/v1/recovery/plan", { method: "POST", body: { item_ids: ids } }) };
  } catch (e) {
    return fail(e);
  }
}

export async function launchBatch(input: { itemIds: string[]; name: string; holdoutPct: number; windowDays: number })
  : Promise<Result<{ batch_id: string }>> {
  const ids = clean(input.itemIds);
  const name = input.name.trim().slice(0, 120);
  if (!ids || !name) return { ok: false, message: "a name and at least one item are required" };
  try {
    const r = await api<{ batch_id: string }>("/v1/recovery/batches", { method: "POST", body: {
      item_ids: ids, name, holdout_pct: Math.min(50, Math.max(0, input.holdoutPct)),
      window_days: Math.min(30, Math.max(1, Math.round(input.windowDays))) } });
    revalidatePath("/recovery");
    return { ok: true, data: r };
  } catch (e) {
    return fail(e);
  }
}

export async function stopBatch(batchId: string): Promise<Result<{ skipped: number }>> {
  if (!/^rb_[0-9A-Z]{26}$/.test(batchId)) return { ok: false, message: "invalid batch id" };
  try {
    const r = await api<{ skipped: number }>(`/v1/recovery/batches/${batchId}/stop`, { method: "POST" });
    revalidatePath(`/recovery/batches/${batchId}`);
    return { ok: true, data: r };
  } catch (e) {
    return fail(e);
  }
}
