"use client";

import { useActionState, useState, useTransition } from "react";
import { toast } from "sonner";
import { Ban, CircleCheck, FilePlus2, Flag, Loader2, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createInvoice, invoiceAction, type FormResult } from "@/lib/billing-actions";

const select = "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs";

export function NewInvoice({ customers, today }: { customers: { customer_id: string; display_name: string | null }[]; today: string }) {
  const [open, setOpen] = useState(false);
  const [state, action, pending] = useActionState<FormResult | null, FormData>(async (p, f) => {
    const r = await createInvoice(p, f);
    if (r.ok) { toast.success(r.message); setOpen(false); }
    return r;
  }, null);
  const [due] = useState(() => new Date(Date.now() + 15 * 864e5).toISOString().slice(0, 10));
  return (
    <>
      <Button size="sm" onClick={() => setOpen(true)} disabled={customers.length === 0}><FilePlus2 />New invoice</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader><DialogTitle>Issue an invoice</DialogTitle>
            <DialogDescription>It is booked as money owed now. The customer gets polite reminders: 3 days before, on the due date, and after.</DialogDescription></DialogHeader>
          <form action={action} className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1 sm:col-span-2"><Label htmlFor="ic">Customer</Label>
              <select id="ic" name="customer_id" className={select} required>
                {customers.map((c) => <option key={c.customer_id} value={c.customer_id}>{c.display_name ?? c.customer_id}</option>)}
              </select></div>
            <div className="space-y-1"><Label htmlFor="in">Invoice number</Label><Input id="in" name="number" required maxLength={40} placeholder="INV-2026-041" /></div>
            <div className="space-y-1"><Label htmlFor="ia">Amount (₹)</Label><Input id="ia" name="amount" type="number" min={1} step="0.01" required /></div>
            <div className="space-y-1"><Label htmlFor="ii">Issued on</Label><Input id="ii" name="issued_on" type="date" required defaultValue={today} /></div>
            <div className="space-y-1"><Label htmlFor="id">Due on</Label><Input id="id" name="due_on" type="date" required defaultValue={due} /></div>
            <div className="space-y-1 sm:col-span-2"><Label htmlFor="ide">Description (optional)</Label><Input id="ide" name="description" maxLength={300} placeholder="October consignment" /></div>
            {state && !state.ok && <p className="text-sm text-danger sm:col-span-2">{state.message}</p>}
            <DialogFooter className="sm:col-span-2">
              <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={pending}>{pending ? <Loader2 className="animate-spin" /> : <FilePlus2 />}Issue invoice</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function InvoiceActions({ invoiceId, status }: { invoiceId: string; status: string }) {
  const [pending, start] = useTransition();
  const run = (action: "send" | "dispute" | "resolve" | "write-off", reason?: string) => start(async () => {
    const r = await invoiceAction(invoiceId, action, reason);
    if (r.ok) toast.success(r.message); else toast.error(r.message);
  });
  const open = status === "open" || status === "partially_paid";
  if (!open && status !== "disputed") return null;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild><Button size="sm" variant="ghost" disabled={pending}>{pending ? <Loader2 className="animate-spin" /> : "Actions"}</Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {open && <DropdownMenuItem onClick={() => run("send")}><Send />Send payment link now</DropdownMenuItem>}
        {open && <DropdownMenuItem onClick={() => { const r = prompt("What does the customer dispute?"); if (r) run("dispute", r); }}><Flag />Mark disputed</DropdownMenuItem>}
        {status === "disputed" && <DropdownMenuItem onClick={() => run("resolve")}><CircleCheck />Dispute resolved</DropdownMenuItem>}
        <DropdownMenuItem onClick={() => { const r = prompt("Why write this off? (finance approvers only)"); if (r) run("write-off", r); }}><Ban />Write off…</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
