"use server";

import { api } from "@/lib/api";
import { switchBusiness } from "@/lib/business-actions";

export type CreateResult = { ok: boolean; message: string };

const SEGMENTS = ["subscription", "saas", "edtech", "media", "fitness", "insurance", "lending", "other"];

/** Create the person's business; the API makes them its owner. */
export async function createBusiness(_: CreateResult | null, formData: FormData): Promise<CreateResult> {
  const name = String(formData.get("name") ?? "").trim();
  const segment = String(formData.get("segment") ?? "subscription");
  if (name.length < 2 || name.length > 80) return { ok: false, message: "Business name must be 2–80 characters" };
  if (!SEGMENTS.includes(segment)) return { ok: false, message: "Choose what kind of business this is" };
  let tenant: string;
  try {
    tenant = (await api<{ tenant_id: string }>("/v1/businesses", { method: "POST", body: { name, segment } })).tenant_id;
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
  const fd = new FormData();
  fd.set("tenant_id", tenant);
  fd.set("return_to", "/setup");
  await switchBusiness(fd);              // act in the new business and continue with its setup
  return { ok: true, message: "created" };
}
