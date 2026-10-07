"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { OctagonX, Printer, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { stopBatch } from "../../actions";

export function BatchControls({ batchId, running }: { batchId: string; running: boolean }) {
  const router = useRouter();
  const [pending, start] = useTransition();
  const stop = () => {
    if (!confirm("Stop this batch? Customers not yet contacted will not be contacted. Already-sent messages stay sent and are still measured.")) return;
    start(async () => {
      const r = await stopBatch(batchId);
      if (r.ok) { toast.success(`Stopped · ${r.data.skipped} customers will not be contacted`); router.refresh(); }
      else toast.error(r.message);
    });
  };
  return (
    <>
      <Button variant="outline" size="sm" onClick={() => router.refresh()}><RefreshCw />Refresh</Button>
      <Button variant="outline" size="sm" onClick={() => window.print()}><Printer />Print report</Button>
      {running && <Button variant="destructive" size="sm" onClick={stop} disabled={pending}><OctagonX />Stop batch</Button>}
    </>
  );
}
