# syntax=docker/dockerfile:1.7
# Nirantar — all-in-one DEMO image (Hugging Face Docker Space, port 7860).
#
# The real product in one container: Postgres (RLS) · Redis · Redpanda · Temporal (dev server) · API · always-on
# services (workflows, relay, event bridge) · web app · Caddy on :7860. Data lives on the container disk and is
# re-seeded with a synthetic business on every start — it is a showcase, not a production deployment.
# NIRANTAR_DEMO=1 is baked in: mock payments only, messages go to the mock sink, voice is off, and the processes
# refuse to start if real WhatsApp / Exotel / Razorpay credentials are present (nirantar.core.demo).
# Production uses deploy/compose.prod.yml (separate containers, persistent volumes, TLS) — see docs/deployment.

# ---------------------------------------------------------------- web (Next.js standalone)
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY apps/web/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---------------------------------------------------------------- python (locked dependencies)
FROM python:3.12-slim-bookworm AS py
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --all-extras --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY ml ./ml
# read at runtime: the compliance policy catalogue (policy engine, RAG) and the latest eval results (API)
COPY docs/compliance ./docs/compliance
COPY evals/results ./evals/results
RUN uv sync --frozen --no-dev --all-extras

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm
ARG BUILD_ID=dev
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg libgomp1 redis-server \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-16 postgresql-16-pgvector \
    && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=redpandadata/redpanda:v26.2.4 /opt/redpanda /opt/redpanda
# the broker runs libexec/redpanda directly; the rpk CLI (Go) is not needed and only adds attack surface
RUN rm -f /opt/redpanda/libexec/rpk /opt/redpanda/bin/rpk
COPY --from=temporalio/temporal:1.9.1 /usr/local/bin/temporal /usr/local/bin/temporal
COPY --from=caddy:2.11.7 /usr/bin/caddy /usr/local/bin/caddy

# Hugging Face runs the container as uid 1000; everything it writes lives under /home/user.
RUN useradd --create-home --uid 1000 user
WORKDIR /app
COPY --from=py --chown=user:user /app /app
COPY --from=web --chown=user:user /web/.next/standalone /web
COPY --from=web --chown=user:user /web/.next/static /web/.next/static
COPY --from=web --chown=user:user /web/public /web/public
COPY --chown=user:user infra/compose/postgres-init/01-roles.sql /app/deploy/space/01-roles.sql
COPY --chown=user:user deploy/space/ /app/deploy/space/
# strip Windows line endings whatever editor last touched these, then make the entrypoint executable
RUN for f in start.sh Caddyfile; do tr -d '\015' < /app/deploy/space/$f > /tmp/$f && cat /tmp/$f > /app/deploy/space/$f; done \
    && chmod +x /app/deploy/space/start.sh

ENV PATH="/app/.venv/bin:/usr/lib/postgresql/16/bin:/opt/redpanda/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/user \
    NIRANTAR_DEMO=1 \
    NIRANTAR_ENV=demo \
    NIRANTAR_COMMS=mock \
    NIRANTAR_BUILD_ID=${BUILD_ID} \
    DATA=/home/user/data
USER user
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/ready || exit 1
STOPSIGNAL SIGTERM
CMD ["/app/deploy/space/start.sh"]
