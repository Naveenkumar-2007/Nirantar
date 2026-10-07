"use client";

import { useState, useTransition } from "react";
import { toast } from "sonner";
import { Download, FileUp, Loader2, Upload } from "lucide-react";
import { Badge } from "@/components/kit";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { importCustomers, type ImportReport } from "@/lib/billing-actions";

const TEMPLATE = "name,phone,language,email,reference,whatsapp_consent,consent_source,plan,start_on\n" +
  "Asha Rao,9876543210,hi,,M-1001,yes,signup form at the counter,Chai Monthly,\n" +
  "Ravi Kumar,9876543211,te,ravi@example.com,M-1002,no,,,\n";

export function ImportCustomers() {
  const [open, setOpen] = useState(false);
  const [csv, setCsv] = useState<string | null>(null);
  const [file, setFile] = useState<string>("");
  const [report, setReport] = useState<ImportReport | null>(null);
  const [pending, start] = useTransition();

  const reset = () => { setCsv(null); setFile(""); setReport(null); };
  const pick = async (f: File | undefined) => {
    if (!f) return;
    if (f.size > 1_000_000) { toast.error("The file must be under 1 MB"); return; }
    const text = await f.text();
    setFile(f.name); setCsv(text); setReport(null);
    start(async () => {
      const r = await importCustomers(text, true);
      if (r.ok) setReport(r.report); else toast.error(r.message);
    });
  };
  const doImport = () => start(async () => {
    if (!csv) return;
    const r = await importCustomers(csv, false);
    if (!r.ok) { toast.error(r.message); return; }
    toast.success(`Imported ${r.report.created} customers${r.report.enrolled ? `, ${r.report.enrolled} put on a plan` : ""}`);
    setOpen(false); reset();
  });

  return (
    <>
      <Button size="sm" variant="outline" onClick={() => { reset(); setOpen(true); }}><FileUp />Import CSV</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Import customers</DialogTitle>
            <DialogDescription>Upload your customer list. Nothing is saved until you press Import — first you see every problem, row by row.</DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap items-center gap-2">
            <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-sm hover:bg-accent">
              <Upload className="size-4" />{file || "Choose a .csv file"}
              <input type="file" accept=".csv,text/csv" className="sr-only" onChange={(e) => pick(e.target.files?.[0])} />
            </label>
            <a className="inline-flex items-center gap-1 text-sm text-primary hover:underline" download="nirantar-customers-template.csv"
              href={`data:text/csv;charset=utf-8,${encodeURIComponent(TEMPLATE)}`}><Download className="size-4" />Template</a>
            {pending && <Loader2 className="size-4 animate-spin text-muted-foreground" />}
          </div>
          <p className="text-xs text-muted-foreground">Required: name, phone. WhatsApp consent must say how the customer agreed. A plan name puts the customer on that plan; start_on is the first due date (default today).</p>
          {report && (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2 text-sm">
                <Badge tone="good">{report.valid} ready</Badge>
                {report.invalid > 0 && <Badge tone="bad">{report.invalid} with problems</Badge>}
                <Badge tone="info">{report.with_consent} with WhatsApp consent</Badge>
                {report.to_enroll > 0 && <Badge tone="info">{report.to_enroll} to put on a plan</Badge>}
              </div>
              {report.problems.length > 0 && (
                <ul className="max-h-64 divide-y divide-border overflow-y-auto rounded-lg border border-border text-sm">
                  {report.problems.map((p) => (
                    <li key={p.line} className="p-2"><b>Row {p.line}</b> {p.name && <span className="text-muted-foreground">· {p.name}</span>}
                      <div className="text-xs text-danger">{p.problems.join(" · ")}</div></li>
                  ))}
                </ul>
              )}
              {report.invalid > 0 && report.valid > 0 && <p className="text-xs text-muted-foreground">Rows with problems are skipped. Fix them in the file and upload again any time.</p>}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button onClick={doImport} disabled={pending || !report || report.valid === 0}>
              {pending ? <Loader2 className="animate-spin" /> : <Upload />}Import {report?.valid ?? 0} customers
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
