"use client";

import { useActionState, useEffect, useRef } from "react";
import { Loader2, SendHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { sendReply, type ReplyResult } from "./actions";

export function ReplyBox({ customerId, windowOpen }: { customerId: string; windowOpen: boolean }) {
  const [state, action, pending] = useActionState<ReplyResult | null, FormData>(sendReply, null);
  const form = useRef<HTMLFormElement>(null);
  useEffect(() => { if (state?.ok) form.current?.reset(); }, [state]);
  if (!windowOpen) {
    return (
      <div className="border-t border-border px-5 py-3 text-sm text-muted-foreground">
        You can reply here once the customer writes again — outside WhatsApp&apos;s 24-hour window only approved templates may be sent, and the agents handle those.
      </div>
    );
  }
  return (
    <form ref={form} action={action} className="flex items-end gap-2 border-t border-border p-3">
      <input type="hidden" name="customer_id" value={customerId} />
      <Textarea name="text" required minLength={2} maxLength={1000} rows={2} placeholder="Reply as yourself — checked by the same compliance rules as the agents"
        className="min-h-0 flex-1 resize-none" />
      <div className="flex flex-col items-end gap-1">
        <Button type="submit" disabled={pending}>{pending ? <Loader2 className="animate-spin" /> : <SendHorizontal />}Send</Button>
        {state && <span className={`max-w-56 text-right text-xs ${state.ok ? "text-success" : "text-danger"}`}>{state.message}</span>}
      </div>
    </form>
  );
}
