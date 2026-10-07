"use server";

import { publicApi } from "@/lib/public-api";

type Rzp = { razorpay_payment_id: string; razorpay_order_id: string; razorpay_signature: string };

/** Forward Razorpay's checkout result to the API, which verifies the signature and fetches the payment itself. */
export async function confirmPayment(token: string, r: Rzp): Promise<{ ok: boolean; message: string }> {
  if (!/^ten_[0-9A-Z]{26}\.prq_[0-9A-Z]{26}\.[0-9a-f]{32}$/.test(token)) return { ok: false, message: "invalid link" };
  try {
    const out = await publicApi<{ status: string }>(`/v1/public/pay/${token}/confirm`, {
      razorpay_order_id: String(r.razorpay_order_id ?? ""), razorpay_payment_id: String(r.razorpay_payment_id ?? ""),
      razorpay_signature: String(r.razorpay_signature ?? "") });
    return out.status === "paid" ? { ok: true, message: "paid" }
      : { ok: false, message: "Payment received but not yet confirmed by the bank. Refresh in a minute." };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "could not confirm the payment" };
  }
}
