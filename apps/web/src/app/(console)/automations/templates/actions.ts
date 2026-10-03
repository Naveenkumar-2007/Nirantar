"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type FormResult = { ok: boolean; message: string };

const KEY = /^[a-z_]+\.[a-z_]+$/;
const LANG = /^[a-z]{2}$/;

/** Author a new version (owner key, policy:admin). The API runs the compliance checks and stores it as pending. */
export async function proposeTemplate(_: FormResult | null, fd: FormData): Promise<FormResult> {
  const key = String(fd.get("key") ?? ""), language = String(fd.get("language") ?? ""), body = String(fd.get("body") ?? "");
  if (!KEY.test(key) || !LANG.test(language)) return { ok: false, message: "invalid template" };
  try {
    const r = await api<{ version: number }>("/v1/templates", { method: "POST", body: { key, language, body } });
    revalidatePath("/automations/templates");
    return { ok: true, message: `Submitted v${r.version} for review` };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}

/** Review a pending version with the approver's key — a different person from the author (maker-checker). */
export async function decideTemplate(_: FormResult | null, fd: FormData): Promise<FormResult> {
  const key = String(fd.get("key") ?? ""), language = String(fd.get("language") ?? "");
  const version = Number(fd.get("version")), grant = fd.get("grant") === "true";
  if (!KEY.test(key) || !LANG.test(language) || !Number.isInteger(version)) return { ok: false, message: "invalid template" };
  try {
    await api(`/v1/templates/${key}/${language}/${version}/decide`, { kind: "approver", method: "POST", body: { grant } });
    revalidatePath("/automations/templates");
    return { ok: true, message: grant ? `v${version} is live` : `v${version} rejected` };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}
