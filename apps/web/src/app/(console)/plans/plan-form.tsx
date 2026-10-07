"use client";

import { useActionState, useEffect, useRef } from "react";
import { Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createPlan, type FormResult } from "@/lib/billing-actions";

const select = "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs";

export function PlanForm() {
  const [state, action, pending] = useActionState<FormResult | null, FormData>(createPlan, null);
  const form = useRef<HTMLFormElement>(null);
  useEffect(() => { if (state?.ok) form.current?.reset(); }, [state]);
  return (
    <form ref={form} action={action} className="space-y-3">
      <div className="space-y-1"><Label htmlFor="pn">Name</Label>
        <Input id="pn" name="name" required minLength={2} maxLength={80} placeholder="Chai Monthly" /></div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1"><Label htmlFor="pa">Price (₹)</Label>
          <Input id="pa" name="amount" type="number" required min={1} max={100000} step="0.01" placeholder="499" /></div>
        <div className="space-y-1"><Label htmlFor="pi">Every</Label>
          <select id="pi" name="interval" className={select} defaultValue="monthly">
            <option value="weekly">week</option><option value="monthly">month</option>
            <option value="quarterly">quarter</option><option value="yearly">year</option>
          </select></div>
      </div>
      <div className="space-y-1"><Label htmlFor="pm">Collect by</Label>
        <select id="pm" name="method" className={select} defaultValue="payment_link">
          <option value="payment_link">Payment link on each due date (works with every account)</option>
          <option value="mandate" disabled>UPI AutoPay / e-mandate (coming soon)</option>
        </select></div>
      <div className="space-y-1"><Label htmlFor="pd">Description (optional)</Label>
        <Input id="pd" name="description" maxLength={300} placeholder="2 cups a day, delivered" /></div>
      <Button type="submit" disabled={pending} className="w-full">{pending ? <Loader2 className="animate-spin" /> : <Plus />}Create plan</Button>
      {state && <p className={`text-sm ${state.ok ? "text-success" : "text-danger"}`}>{state.message}</p>}
    </form>
  );
}
