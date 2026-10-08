# Hugging Face Space (public demo)

`Naveen-2007/nirantar` is a Docker Space running the **demo**: the whole product in one container
(`./Dockerfile`, entrypoint `deploy/space/start.sh`), listening on port 7860.

## Why the Space is a demo, not production

- **No durable disk.** A Space's disk is wiped on every restart, but the ledger, audit chain and workflow history
  must never be lost.
- **One container.** Production separates the API, the workers, the database and the queues, so each can be
  scaled, restarted and backed up on its own.
- **Sleeps.** Free Spaces sleep when idle, but webhooks from Razorpay, WhatsApp and Exotel must always be received.

Production runs on a VPS with `deploy/compose.prod.yml`; see `deploy/README.md`.

## What the container does on every start

1. Creates a fresh data directory and random per-start secrets (database passwords, API key pepper, signing keys,
   data encryption key).
2. Starts Postgres 16 with pgvector and the RLS roles, Redis, Redpanda and the Temporal
   dev server.
3. Runs `alembic upgrade head`.
4. Seeds "Chai Club (demo)" through the real pipeline (`nirantar.demo.seed`).
5. Starts the API, the always-on services, the web app and Caddy on :7860.

If any process exits, the container exits and the platform restarts it from a clean slate.

## Deploying

The GitHub Action `deploy-huggingface` runs after CI passes on `main`:

1. Bundles the Space files with `scripts/space_bundle.sh`. The bundle contains no tests, data or env files.
2. Uploads the bundle to the Space.
3. Waits for the build.
4. Runs `scripts/space_smoke.py`: `/ready`, `/version` reports demo mode, the web app loads, and an unsigned
   webhook is refused.
5. If the smoke test fails, re-uploads the previously deployed commit.

**One-time setup:**

- Create a fine-grained Hugging Face token with write access to the Space only.
- Add it as the repository secret `HF_TOKEN`, in a GitHub environment named `huggingface-demo`.
- Add **no other secrets to the Space.** The demo refuses real WhatsApp, Exotel or Razorpay credentials.

**Manual deploy from a machine with `hf auth login`:**

```bash
bash scripts/space_bundle.sh /tmp/space && hf upload Naveen-2007/nirantar /tmp/space . --repo-type space --delete "*"
```
