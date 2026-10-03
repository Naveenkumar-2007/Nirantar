"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

export type TeamResult = { ok: boolean; message: string };

const ROLES = ["owner", "finance_approver", "compliance_officer", "ops_analyst", "viewer"];

export async function inviteMember(_: TeamResult | null, formData: FormData): Promise<TeamResult> {
  const email = String(formData.get("email") ?? "").trim().toLowerCase();
  const role = String(formData.get("role") ?? "viewer");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return { ok: false, message: "Enter a valid email address" };
  if (!ROLES.includes(role)) return { ok: false, message: "Choose a role" };
  try {
    await api("/v1/team/invites", { method: "POST", body: { email, role } });
    revalidatePath("/settings/team");
    return { ok: true, message: `Invited ${email}. They join when they sign in with that verified email.` };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message.replace(/^\d+ /, "") : "failed" };
  }
}

export async function revokeInvite(formData: FormData): Promise<void> {
  const id = String(formData.get("invite_id") ?? "");
  if (/^inv_[0-9A-Z]{26}$/.test(id)) await api(`/v1/team/invites/${id}`, { method: "DELETE" });
  revalidatePath("/settings/team");
}
