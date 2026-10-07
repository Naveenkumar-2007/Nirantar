# Deploying Nirantar (pilot: one server)

One Linux server (4 vCPU / 8 GB RAM / 80 GB disk is comfortable; 4 GB works without the ML extras), Docker Engine
with the compose plugin, and a domain you control.

## 1. DNS

Point two records at the server's IP:

| Record | Type | Value |
|---|---|---|
| `app.example.in` (your `NIRANTAR_DOMAIN`) | A | server IP |
| `auth.app.example.in` | A | server IP |

Caddy obtains HTTPS certificates automatically once both names resolve.

## 2. Secrets

```bash
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
openssl rand -base64 32        # run once per password/secret field
echo "k1:$(openssl rand -base64 32)"   # DATA_KEYS (32-byte key, id k1); rotate by prepending k2:...
```

Fill **every** field. The real files never leave the server and are never committed.

## 3. Start

```bash
docker compose -f deploy/compose.prod.yml --env-file deploy/.env up -d --build
docker compose -f deploy/compose.prod.yml --env-file deploy/.env ps
```

`migrate` runs `alembic upgrade head` and exits. The API and services start after it. Check:

- `https://<domain>/health`: `{"db": "ok", ...}`
- `https://<domain>`: sign-in page, then "Create account", then onboarding

## 4. Connect providers (per business, inside the app)

- **Razorpay:** Setup, then Connect Razorpay with **test keys first**. In the Razorpay dashboard, add the webhook URL
  the app shows (`https://<domain>/webhooks/razorpay/<business id>`) with the generated secret.
- **WhatsApp Cloud API:**
  - Meta app, then Webhooks: callback `https://<domain>/webhooks/whatsapp`, verify token = `WHATSAPP_VERIFY_TOKEN`.
  - Subscribe to `messages`.
  - Submit templates: `docker compose ... exec api python -m nirantar.channels.wa_templates submit`, then `check`.

## 5. Operate

- **Backups:** nightly `pg_dump` of every database into the `backups` volume, kept `BACKUP_KEEP_DAYS` days.
  **Copy them off the server** (`rclone` to object storage); a backup on the same disk is not a backup.
  - Restore: `pg_restore -h postgres -U nirantar_owner -d nirantar --clean <file>`.
- **Updates:** `git pull && docker compose ... up -d --build`. Migrations run first. Running workflows are protected
  by replay tests (`tests/replay`).
- **Keycloak admin** is not exposed on the internet:
  `ssh -L 8080:localhost:8080 server` plus a temporary port mapping, or `docker compose exec keycloak ...`.
- **Logs:** `docker compose ... logs -f api services`. Caddy logs JSON access lines.

## What is public

| Path | Who | Protection |
|---|---|---|
| `/pay/<token>` | customers | unguessable signed token, minimal data, rate-limited, payment confirmed only by provider signature + fetch |
| `/webhooks/*` | Razorpay, Meta | signature verification, rate-limited, 2 MB body cap |
| everything else | business users | Keycloak sign-in, per-business roles, Postgres row-level security |

The API (`/v1/*`) is **not** routed from the internet; the web server calls it on the private network.
