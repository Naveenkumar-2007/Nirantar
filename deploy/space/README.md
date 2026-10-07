---
title: Nirantar
emoji: 🔁
colorFrom: gray
colorTo: green
sdk: docker
app_port: 7860
pinned: false
short_description: Continuous, measured revenue recovery for Indian businesses
---

# Nirantar — live demo

**Nirantar** (निरंतर, "continuous") recovers revenue that businesses lose to failed subscription payments, bank
outages, broken promises-to-pay and overdue invoices, and proves how much it recovered against a control group.

This Space runs the **real product** in a single container, with one synthetic business ("Chai Club (demo)")
seeded through the real pipeline on every start: signed provider webhooks → ledger → AI triage → policy-checked
recovery actions → verified settlement → measured uplift.

**Demo mode is enforced in code** (`nirantar.core.demo`):

- payments use the mock provider only — no real money moves;
- WhatsApp messages go to a mock sink and voice calls are disabled — nobody is ever contacted;
- the container refuses to start if real WhatsApp, Exotel or Razorpay credentials are present;
- all data is reset on restart.

Source and production deployment: see the GitHub repository (`deploy/compose.prod.yml`, `docs/deployment`).
