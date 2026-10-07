# ADR-0025: B2B receivables — invoices, partial payments, a polite ladder with a person before the final word (P10)

- Status: Accepted · Date: 2026-10-07
- Brief direction: "B2B receivables chaser".
- Evidence: `tests/e2e/test_receivables.py`. Three invoices to one distributor, run over 30+ simulated days on
  Temporal:
  - **X:** reminded before due, pays ₹4,000 of ₹10,000; the follow-up asks only for the ₹6,000 still owed; X pays
    it; verified, ledgered, the ladder stops.
  - **Z:** disputed, so the ladder stops at its next check.
  - **Y:** never pays. Follow-ups go out within the contact budget (some are refused, with the reason recorded).
    The final notice waits for a person's approval; at +30 days a collections case opens.
  - Views and permissions: ageing reports only Y as outstanding. Write-off is refused for an ops analyst and
    allowed for the owner, with a reason.

## Decisions

1. **An invoice is money owed from the moment it is issued.**
   - Issue: DR receivable / CR income (idempotent on the invoice).
   - Payment: DR clearing / CR receivable, only after `verify_capture` confirms that exact amount with the provider.
   - Partial payments are normal; the invoice is `paid` when nothing is outstanding.
2. **Collection reuses the billing loop.**
   - Payment requests may target an invoice (`payment_requests.invoice_id`, exactly one subject).
   - Every request is for the outstanding amount only.
   - Provider links, the hosted pay page, polling, webhooks (by our reference or the order id) and the pay-page
     confirmation all route invoice payments to `receivables.apply_payment`.
3. **The ladder** (InvoiceChaseWorkflow), in days from the due date: reminder −3 · due 0 · overdue_1 +3 ·
   overdue_2 +7 · final +14 · human +30.
   - It runs at 10:00 IST on each step's date and checks the provider every 6 hours in between.
   - It stops when the invoice is paid, disputed, written off or cancelled.
   - Each step runs as the `receivables_agent` through the gateway (consent, window, fatigue, conduct, audit).
   - **The final notice is `approval="always"`.** "Human" opens a `collections` case and ends the automation.
4. **Courteous wording, in three languages.**
   - Reminder; overdue, with "if you have already paid, please ignore this"; final, with "reply so we can find a
     way together" and a deadline.
   - No threats; the template registry's conduct screen is enforced.
5. **Disputes and write-offs are people's decisions.**
   - Dispute pauses chasing (`subscriptions:write`); resolve resumes.
   - Write-off needs `approvals:decide` and a reason, and is audited.
6. **Proof.** Ageing buckets (not due / 1–30 / 31–60 / 61–90 / 90+), average days to get paid (90 days), and
   verified money collected (30 days).

## Consequences

- One customer with several invoices shares one contact budget. The 2026-10-07 amendment below sends one
  statement instead of a message per invoice.
- Inbound WhatsApp replies are routed to debits today. Linking a B2B customer's reply ("will pay Friday") to an
  invoice promise is a follow-up.
- Invoice PDFs and GST e-invoicing are out of scope. Nirantar collects against invoices the business already issues.

## Amendment (2026-10-07): customer statements, deferred steps, single execution

1. **One statement per customer per day.**
   - When a non-final step runs and the customer has two or more open invoices, the ladder calls
     `billing.send_invoice_statement` with the key `statement:{customer}:{date}`.
   - The statement is one WhatsApp message listing every open, non-disputed invoice (oldest due first), with one
     "pay all" link for the total owed.
   - Every ladder that reaches the same day shares that one action; the second records its step as `bundled`.
   - An open statement for the same invoice set and amount is reused, not re-created.
   - Final notices stay per invoice and approval-gated.
2. **Pay-all allocation** (`receivables.allocate`, migration 0030).
   - A statement's request carries `invoice_ids`. One payment is verified once and booked once (one ledger entry).
   - The payment is then split oldest-due-first across the invoices, and each split is stored in
     `billing.payment_allocations`.
   - A partial payment pays the oldest invoice first. Replays of the same webhook change nothing.
3. **Deferred, not lost.**
   - A step that comes due outside the contact window (an invoice issued at 11 pm, a worker that was down) is
     denied with `retry_after`.
   - The ladder waits until then and retries, at most twice. This is replay-safe behind `invoice-defer-denied-step`.
4. **The gateway never runs one action twice.**
   - Calls sharing an idempotency key are serialised with a transaction advisory lock.
   - An allowed action is marked `executing` before its handler runs; a concurrent caller gets `in_progress`.
   - A lease of 10 minutes frees an action left behind by a crashed worker.
   - This was found by two ladders sending the same statement at the same moment.
