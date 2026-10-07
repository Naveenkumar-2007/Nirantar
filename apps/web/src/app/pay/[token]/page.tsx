import { BadgeCheck, LockKeyhole } from "lucide-react";
import { publicApi, PublicApiError } from "@/lib/public-api";
import { PayButton } from "./pay-button";

type View = { business: string; plan: string; amount_minor: number; due: string; first_name: string; language: string;
  status: "open" | "paid" | "closed"; order_id: string | null; checkout_key: string | null };

const T = {
  en: { hi: (n: string) => `Hi ${n},`, due: "Payment due", on: "due on", pay: "Pay", paid: "Paid — thank you!",
    paidNote: "Your payment is confirmed with the bank. You can close this page.", closed: "This payment link has expired.",
    closedNote: "Please ask the business for a new link.", secure: "Secured by Razorpay. Nirantar never sees your card or UPI PIN.",
    invalid: "This payment link is not valid." },
  hi: { hi: (n: string) => `नमस्ते ${n},`, due: "भुगतान देय", on: "देय तिथि", pay: "भुगतान करें", paid: "भुगतान हो गया — धन्यवाद!",
    paidNote: "आपका भुगतान बैंक से पुष्टि हो गया है। आप यह पेज बंद कर सकते हैं।", closed: "यह भुगतान लिंक समाप्त हो गया है।",
    closedNote: "कृपया व्यवसाय से नया लिंक माँगें।", secure: "Razorpay द्वारा सुरक्षित। Nirantar आपका कार्ड या UPI PIN कभी नहीं देखता।",
    invalid: "यह भुगतान लिंक मान्य नहीं है।" },
  te: { hi: (n: string) => `నమస్తే ${n},`, due: "చెల్లింపు బాకీ", on: "చెల్లించాల్సిన తేదీ", pay: "చెల్లించండి",
    paid: "చెల్లింపు పూర్తైంది — ధన్యవాదాలు!", paidNote: "మీ చెల్లింపు బ్యాంక్ ద్వారా నిర్ధారించబడింది. ఈ పేజీని మూసివేయవచ్చు.",
    closed: "ఈ చెల్లింపు లింక్ గడువు ముగిసింది.", closedNote: "దయచేసి వ్యాపారాన్ని కొత్త లింక్ అడగండి.",
    secure: "Razorpay ద్వారా సురక్షితం. Nirantar మీ కార్డ్ లేదా UPI PIN ను ఎప్పుడూ చూడదు.", invalid: "ఈ చెల్లింపు లింక్ చెల్లదు." },
} as const;

export const metadata = { title: "Pay", robots: { index: false, follow: false } };

export default async function PayPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  let v: View | null = null;
  try {
    v = await publicApi<View>(`/v1/public/pay/${encodeURIComponent(token)}`);
  } catch (e) {
    if (!(e instanceof PublicApiError && e.status === 404)) throw e;
  }
  const t = T[(v?.language as keyof typeof T) ?? "en"] ?? T.en;
  const amount = v ? `₹${(v.amount_minor / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}` : "";
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4 py-10">
      <div className="w-full max-w-sm rounded-2xl border border-border bg-card p-6 shadow-card">
        {!v ? <p className="text-center text-sm text-muted-foreground">{T.en.invalid}</p> : (
          <>
            <div className="text-xs font-medium uppercase tracking-wider text-primary">{v.business}</div>
            <p className="mt-4 text-sm text-muted-foreground">{t.hi(v.first_name)}</p>
            <div className="mt-1 text-sm text-muted-foreground">{t.due} · {v.plan}</div>
            <div className="mt-2 text-4xl font-semibold tracking-tight tabular-nums">{amount}</div>
            <div className="mt-1 text-xs text-muted-foreground">{t.on} {new Date(v.due).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}</div>
            <div className="mt-6">
              {v.status === "paid" ? (
                <div className="rounded-xl bg-success-soft p-4 text-success">
                  <div className="flex items-center gap-2 font-medium"><BadgeCheck className="size-5" />{t.paid}</div>
                  <p className="mt-1 text-sm">{t.paidNote}</p>
                </div>
              ) : v.status === "closed" || !v.order_id || !v.checkout_key ? (
                <div className="rounded-xl bg-muted p-4 text-sm"><b>{t.closed}</b><p className="mt-1 text-muted-foreground">{t.closedNote}</p></div>
              ) : (
                <PayButton token={token} orderId={v.order_id} checkoutKey={v.checkout_key} amountMinor={v.amount_minor}
                  business={v.business} plan={v.plan} label={`${t.pay} ${amount}`} />
              )}
            </div>
            <p className="mt-6 flex items-start gap-1.5 text-xs text-muted-foreground"><LockKeyhole className="mt-0.5 size-3.5 shrink-0" />{t.secure}</p>
          </>
        )}
      </div>
    </main>
  );
}
