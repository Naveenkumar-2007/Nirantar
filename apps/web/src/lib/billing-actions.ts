"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { api } from "@/lib/api";

/** Plans, customers and enrollment (P8.6). Every call runs on the server with the signed-in person's token; the API
 * enforces roles, validates input again, encrypts contact details and writes the audit chain. */
export type FormResult = { ok: boolean; message: string; data?: Record<string, unknown> };

const msg = (e: unknown) => (e instanceof Error ? e.message : "failed");

export async function createPlan(_: FormResult | null, f: FormData): Promise<FormResult> {
  const amount = Number(f.get("amount"));
  if (!Number.isFinite(amount) || amount < 1) return { ok: false, message: "enter an amount of at least ₹1" };
  try {
    await api("/v1/plans", { method: "POST", body: {
      name: String(f.get("name") ?? "").trim(), amount_rupees: amount, interval: String(f.get("interval") ?? "monthly"),
      collection_method: String(f.get("method") ?? "payment_link"),
      description: String(f.get("description") ?? "").trim() || null } });
    revalidatePath("/plans");
    return { ok: true, message: "Plan created" };
  } catch (e) {
    return { ok: false, message: msg(e) };
  }
}

export async function createCustomer(_: FormResult | null, f: FormData): Promise<FormResult> {
  const digits = String(f.get("phone") ?? "").replace(/[\s-]/g, "");
  const phone = digits.startsWith("+") ? digits : /^[6-9]\d{9}$/.test(digits) ? `+91${digits}` : digits;
  const consent = f.get("consent") === "on";
  let id: string;
  try {
    const r = await api<{ customer_id: string }>("/v1/customers", { method: "POST", body: {
      name: String(f.get("name") ?? "").trim(), phone, email: String(f.get("email") ?? "").trim() || null,
      language: String(f.get("language") ?? "en"), whatsapp_consent: consent,
      consent_source: consent ? String(f.get("source") ?? "").trim() || null : null,
      reference: String(f.get("reference") ?? "").trim() || null } });
    id = r.customer_id;
  } catch (e) {
    return { ok: false, message: msg(e) };
  }
  revalidatePath("/customers");
  redirect(`/customers/${id}`);
}

export async function enrollCustomer(_: FormResult | null, f: FormData): Promise<FormResult> {
  const customer = String(f.get("customer_id") ?? "");
  try {
    const r = await api<{ debits_created: string[]; next_charge_on: string }>("/v1/subscriptions", { method: "POST",
      body: { customer_id: customer, plan_id: String(f.get("plan_id") ?? ""), start_on: String(f.get("start_on") ?? "") } });
    revalidatePath(`/customers/${customer}`);
    return { ok: true, message: r.debits_created.length
      ? "Enrolled. The first payment is due soon: the payment link goes out on the due date."
      : `Enrolled. The first payment link goes out on ${r.next_charge_on}.` };
  } catch (e) {
    return { ok: false, message: msg(e) };
  }
}

export async function sendPaymentLink(debitId: string, customerId: string): Promise<FormResult> {
  if (!/^dbt_[0-9A-Z]{26}$/.test(debitId)) return { ok: false, message: "invalid debit" };
  try {
    const r = await api<{ url: string; sent: boolean; channel_error?: string | null }>(
      `/v1/debits/${debitId}/payment-request`, { method: "POST" });
    revalidatePath(`/customers/${customerId}`);
    return { ok: true, message: r.sent ? "Payment link sent on WhatsApp" : `Link ready — WhatsApp not sent (${r.channel_error ?? "not connected"}). Copy and share it.`,
      data: { url: r.url } };
  } catch (e) {
    const raw = msg(e);
    return { ok: false, message: raw.match(/"reason":\s*"([^"]+)"/)?.[1] ?? raw };
  }
}

export async function checkPayments(path: string): Promise<FormResult> {
  try {
    const r = await api<{ scanned: number; changed: number; errors: string[] }>("/v1/payments/check", { method: "POST" });
    revalidatePath(path);
    return { ok: r.errors.length === 0, message: r.changed
      ? `${r.changed} payment update(s) found and verified` : r.errors[0] ?? "No new payments yet" };
  } catch (e) {
    return { ok: false, message: msg(e) };
  }
}

export async function cancelSubscription(subscriptionId: string, customerId: string): Promise<FormResult> {
  if (!/^sub_[0-9A-Z]{26}$/.test(subscriptionId)) return { ok: false, message: "invalid subscription" };
  try {
    await api(`/v1/subscriptions/${subscriptionId}/cancel`, { method: "POST" });
    revalidatePath(`/customers/${customerId}`);
    return { ok: true, message: "Subscription cancelled; no further payments will be requested" };
  } catch (e) {
    return { ok: false, message: msg(e) };
  }
}

export type ImportReport = {
  rows: number; valid: number; invalid: number; with_consent: number; to_enroll: number; dry_run: boolean;
  problems: { line: number; name: string; problems: string[] }[]; created?: number; enrolled?: number; debits_created?: number;
};

/** CSV import: the dry run validates every row and writes nothing; the real run imports the valid rows. */
export async function importCustomers(csv: string, dryRun: boolean): Promise<{ ok: true; report: ImportReport } | { ok: false; message: string }> {
  if (csv.length < 10 || csv.length > 1_000_000) return { ok: false, message: "the file must be a CSV under 1 MB" };
  try {
    const report = await api<ImportReport>("/v1/customers/import", { method: "POST", body: { csv, dry_run: dryRun } });
    if (!dryRun) revalidatePath("/customers");
    return { ok: true, report };
  } catch (e) {
    return { ok: false, message: msg(e) };
  }
}

/** B2B receivables (ADR-0025). */
export async function createInvoice(_: FormResult | null, f: FormData): Promise<FormResult> {
  const amount = Number(f.get("amount"));
  if (!Number.isFinite(amount) || amount < 1) return { ok: false, message: "enter an amount of at least ₹1" };
  try {
    await api("/v1/invoices", { method: "POST", body: {
      customer_id: String(f.get("customer_id") ?? ""), number: String(f.get("number") ?? "").trim(), amount_rupees: amount,
      issued_on: String(f.get("issued_on") ?? ""), due_on: String(f.get("due_on") ?? ""),
      description: String(f.get("description") ?? "").trim() || null } });
    revalidatePath("/receivables");
    return { ok: true, message: "Invoice issued — the reminder ladder has started" };
  } catch (e) {
    return { ok: false, message: msg(e) };
  }
}

export async function invoiceAction(invoiceId: string, action: "send" | "dispute" | "resolve" | "write-off",
  reason?: string): Promise<FormResult> {
  if (!/^ivc_[0-9A-Z]{26}$/.test(invoiceId)) return { ok: false, message: "invalid invoice" };
  try {
    const body = action === "dispute" || action === "write-off" ? { reason: (reason ?? "").slice(0, 300) || null } : undefined;
    const r = await api<{ sent?: boolean; channel_error?: string | null }>(`/v1/invoices/${invoiceId}/${action}`,
      { method: "POST", body });
    revalidatePath("/receivables");
    const done: Record<string, string> = {
      send: r.sent ? "Payment request sent" : `Link ready, not sent (${r.channel_error ?? "channel unavailable"})`,
      dispute: "Marked disputed: reminders paused", resolve: "Dispute resolved: reminders resume", "write-off": "Written off" };
    return { ok: true, message: done[action] };
  } catch (e) {
    const raw = msg(e);
    return { ok: false, message: raw.match(/"reason":\s*"([^"]+)"/)?.[1] ?? raw };
  }
}
