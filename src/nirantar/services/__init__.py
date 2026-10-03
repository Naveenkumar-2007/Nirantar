"""Always-on services (P5, ADR-0015): Temporal worker, outbox relay and the event bridge, run by one process
(`python -m nirantar.services`) with a health endpoint. Each part is also usable on its own (tests, scaling out)."""
