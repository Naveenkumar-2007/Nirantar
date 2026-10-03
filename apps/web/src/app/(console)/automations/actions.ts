"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";
import type { RiskThresholdSettings, SettingsResponse } from "./types";

export type FormResult = { ok: boolean; message: string };

const NAMESPACES = ["channels", "strategy", "effects", "experiments", "policy"] as const;
type Namespace = (typeof NAMESPACES)[number];
const ARMS = ["whatsapp", "voice"] as const;

function num(fd: FormData, name: string): number {
  const raw = String(fd.get(name) ?? "").trim();
  const v = Number(raw);
  if (raw === "" || !Number.isFinite(v)) throw new Error(`${name}: enter a number`);
  return v;
}
const paise = (rupees: number) => Math.round(rupees * 100);

/** Build the full namespace document from the form. The API validates it again (types, ranges, and for `policy`
 * that every change only TIGHTENS the regulatory baseline), so this is convenience, not the safety check. */
function buildValue(ns: Namespace, fd: FormData, current: SettingsResponse): Record<string, unknown> {
  const cur = current.namespaces[ns].value as {
    risk_threshold?: RiskThresholdSettings; prior?: Record<string, Record<string, number>>;
  };
  switch (ns) {
    case "channels":
      return {
        capacity_per_round: Object.fromEntries(ARMS.map((a) => [a, num(fd, `capacity.${a}`)])),
        cost_minor: Object.fromEntries(ARMS.map((a) => [a, paise(num(fd, `cost.${a}`))])),
      };
    case "strategy":
      return {
        risk_threshold: {
          ...cur.risk_threshold,
          mode: String(fd.get("mode")),
          fixed: num(fd, "fixed"),
          flag_cost_minor: paise(num(fd, "flag_cost")),
          prevention_share: num(fd, "prevention_pct") / 100,
          min_labels: num(fd, "min_labels"),
        },
      };
    case "effects": {
      const prior: Record<string, Record<string, number>> = {};
      for (const cat of Object.keys(cur.prior ?? {})) {
        prior[cat] = Object.fromEntries(ARMS.map((a) => [a, num(fd, `prior.${cat}.${a}`) / 100]));
      }
      return { mode: String(fd.get("mode")), prior_strength: num(fd, "prior_strength"),
               min_per_group: num(fd, "min_per_group"), prior };
    }
    case "experiments":
      return { holdout_bp: Math.round(num(fd, "holdout_pct") * 100), holdout_opt_in: fd.get("holdout_opt_in") === "on" };
    case "policy": {
      // Store only what differs from the platform/regulatory baseline.
      const base = current.policy_platform;
      const next: Record<string, unknown> = {
        contact_window: [String(fd.get("window_start")), String(fd.get("window_end"))],
        max_contacts_7d: num(fd, "max_contacts_7d"),
        refund_approval_above_minor: paise(num(fd, "refund_approval")),
        representment_approval_above_minor: paise(num(fd, "representment_approval")),
        discount_approval_above_minor: paise(num(fd, "discount_approval")),
        predebit_notice_hours: num(fd, "predebit_notice_hours"),
        allow_debit_shift: fd.get("allow_debit_shift") === "on",
        voice_registered: fd.get("voice_registered") === "on",
        lending_collections_enabled: fd.get("lending_collections_enabled") === "on",
      };
      return Object.fromEntries(Object.entries(next).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(base[k])));
    }
  }
}

export async function saveSettings(_: FormResult | null, fd: FormData): Promise<FormResult> {
  const ns = String(fd.get("namespace")) as Namespace;
  if (!NAMESPACES.includes(ns)) return { ok: false, message: "unknown settings section" };
  const reason = String(fd.get("reason") ?? "").trim();
  if (reason.length < 3) return { ok: false, message: "Say why you are changing this (kept in the audit trail)." };
  try {
    const current = await api<SettingsResponse>("/v1/settings");
    const value = buildValue(ns, fd, current);
    const r = await api<{ version: number }>(`/v1/settings/${ns}`, {
      method: "PUT", body: { value, reason, expected_version: Number(fd.get("version")) } });
    revalidatePath("/automations");
    return { ok: true, message: `Saved as version ${r.version}` };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}

export async function refreshLearned(): Promise<FormResult> {
  try {
    const r = await api<{ effects: { version: number }; risk_threshold: { value: { threshold: number | null } } }>(
      "/v1/learned/refresh", { method: "POST" });
    revalidatePath("/automations");
    const t = r.risk_threshold.value.threshold;
    return { ok: true, message: `Re-learned (effects v${r.effects.version}; threshold ${t ?? "needs more data"})` };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "failed" };
  }
}
