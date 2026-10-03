"use server";

import { redirect } from "next/navigation";
import { api } from "@/lib/api";

export type CreateResult = { ok: boolean; message: string };

const SEGMENTS = ["subscription", "saas", "edtech", "media", "fitness", "insurance", "lending", "other"];

/** Create the person's business; the API makes them its owner. */
export async function createBusiness(_: CreateResult | null, formData: FormData): Promise<CreateResult> {
  const name = String(formData.get("name") ?? "").trim();
  const segment = String(formData.get("segment") ?? "subscription");
  if (name.length < 2 || name.length > 80) return { ok: false, message: "Business name must be 2–80 characters" };
  if (!SEGMENTS.includes(segment)) return { ok: false, message: "Choose what kind of business this is" };
  try {
    await api<{ tenant_id: string }>("/v1/businesses", { method: "POST", body: { name, segment } });
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
  redirect("/");
}
