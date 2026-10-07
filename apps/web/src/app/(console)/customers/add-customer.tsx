"use client";

import { useActionState, useState } from "react";
import { Loader2, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createCustomer, type FormResult } from "@/lib/billing-actions";

const select = "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs";

export function AddCustomer() {
  const [open, setOpen] = useState(false);
  const [consent, setConsent] = useState(false);
  const [state, action, pending] = useActionState<FormResult | null, FormData>(createCustomer, null);
  return (
    <>
      <Button size="sm" onClick={() => setOpen(true)}><UserPlus />Add customer</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Add a customer</DialogTitle>
            <DialogDescription>Phone and email are encrypted with your business&apos;s own key. Nirantar messages a customer only with their consent.</DialogDescription>
          </DialogHeader>
          <form action={action} className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1 sm:col-span-2"><Label htmlFor="cn">Name</Label>
                <Input id="cn" name="name" required minLength={2} maxLength={80} placeholder="Asha Rao" /></div>
              <div className="space-y-1"><Label htmlFor="cp">Mobile (WhatsApp)</Label>
                <Input id="cp" name="phone" required inputMode="tel" placeholder="98765 43210" /></div>
              <div className="space-y-1"><Label htmlFor="cl">Language</Label>
                <select id="cl" name="language" className={select} defaultValue="en">
                  <option value="en">English</option><option value="hi">हिन्दी</option><option value="te">తెలుగు</option>
                </select></div>
              <div className="space-y-1"><Label htmlFor="ce">Email (optional)</Label>
                <Input id="ce" name="email" type="email" maxLength={200} /></div>
              <div className="space-y-1"><Label htmlFor="cr">Your reference (optional)</Label>
                <Input id="cr" name="reference" maxLength={64} placeholder="member #1042" /></div>
            </div>
            <label className="flex items-start gap-2 rounded-lg border border-border p-3 text-sm">
              <input type="checkbox" name="consent" checked={consent} onChange={(e) => setConsent(e.target.checked)} className="mt-0.5 size-4 accent-[var(--primary)]" />
              <span>The customer agreed to receive payment reminders and links on WhatsApp.</span>
            </label>
            {consent && <div className="space-y-1"><Label htmlFor="cs">How did they agree?</Label>
              <Input id="cs" name="source" required maxLength={200} placeholder="signup form at the counter, website checkbox…" /></div>}
            {state && !state.ok && <p className="text-sm text-danger">{state.message}</p>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={pending}>{pending ? <Loader2 className="animate-spin" /> : <UserPlus />}Add customer</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
