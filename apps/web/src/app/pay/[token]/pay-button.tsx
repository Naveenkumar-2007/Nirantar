"use client";

import { useState } from "react";
import Script from "next/script";
import { useRouter } from "next/navigation";
import { Loader2, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { confirmPayment } from "./actions";

type RazorpayResponse = { razorpay_payment_id: string; razorpay_order_id: string; razorpay_signature: string };
type RazorpayCtor = new (o: Record<string, unknown>) => { open: () => void; on: (e: string, f: (r: unknown) => void) => void };

/** Razorpay Standard Checkout (official checkout.js). The browser gets only the public key id and the order id;
 * the result is confirmed on the server: signature verified with the key secret, payment fetched from Razorpay. */
export function PayButton({ token, orderId, checkoutKey, amountMinor, business, plan, label }: {
  token: string; orderId: string; checkoutKey: string; amountMinor: number; business: string; plan: string;
  label: string;
}) {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const open = () => {
    const Razorpay = (window as unknown as { Razorpay?: RazorpayCtor }).Razorpay;
    if (!Razorpay) { setError("Payment window did not load. Check your connection and try again."); return; }
    setError(null);
    setBusy(true);
    const rzp = new Razorpay({
      key: checkoutKey, order_id: orderId, amount: amountMinor, currency: "INR", name: business, description: plan,
      theme: { color: "#0f766e" },
      handler: async (r: RazorpayResponse) => {
        const res = await confirmPayment(token, r);
        setBusy(false);
        if (res.ok) router.refresh(); else setError(res.message);
      },
      modal: { ondismiss: () => setBusy(false) },
    });
    rzp.on("payment.failed", () => { setBusy(false); setError("The payment did not go through. You can try again."); });
    rzp.open();
  };

  return (
    <>
      <Script src="https://checkout.razorpay.com/v1/checkout.js" strategy="afterInteractive" onReady={() => setReady(true)} />
      <Button className="h-12 w-full text-base" onClick={open} disabled={!ready || busy} aria-label={label}>
        {busy ? <Loader2 className="animate-spin" /> : <ShieldCheck />}{label}
      </Button>
      {error && <p className="mt-3 text-sm text-danger" role="alert">{error}</p>}
    </>
  );
}
