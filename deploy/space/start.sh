#!/usr/bin/env bash
# Nirantar demo container entrypoint: start the real stack, seed one synthetic business, serve on :7860.
# Every start is a clean slate (data under $DATA is recreated); secrets are random per start and never leave the
# container. If any long-running process exits, the container exits so the platform restarts it.
set -Eeuo pipefail

DATA="${DATA:-/home/user/data}"
APP=/app
SPACE="$APP/deploy/space"
log() { printf '{"at":"%s","component":"start","msg":"%s"}\n' "$(date -u +%FT%TZ)" "$*"; }
rand() { python -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'; }

PIDS=()
shutdown() {
  log "shutting down"
  for pid in "${PIDS[@]:-}"; do kill -TERM "$pid" 2>/dev/null || true; done
  pg_ctl -D "$DATA/pg" -m fast stop >/dev/null 2>&1 || true
  wait || true
}
trap shutdown TERM INT

wait_for() {  # wait_for <name> <seconds> <command...>
  local name=$1 secs=$2; shift 2
  for _ in $(seq 1 "$secs"); do timeout 5 "$@" >/dev/null 2>&1 && { log "$name ready"; return 0; }; sleep 1; done
  log "$name did not become ready in ${secs}s"; return 1
}

rm -rf "$DATA" && mkdir -p "$DATA"/{pgsock,redpanda/data,s3,web}

# ---------------------------------------------------------------- secrets (random per start)
OWNER_PW=$(rand); APP_PW=$(rand)
export API_KEY_PEPPER=$(rand) APPROVAL_SIGNING_KEY=$(rand) PLATFORM_ADMIN_KEY=$(rand)
export DATA_KEYS="demo:$(python -c 'import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())')"
export DATABASE_OWNER_URL="postgresql+psycopg://nirantar_owner:${OWNER_PW}@127.0.0.1:5432/nirantar"
export DATABASE_URL="postgresql+psycopg://nirantar_app:${APP_PW}@127.0.0.1:5432/nirantar"
export RELAY_DATABASE_URL="$DATABASE_OWNER_URL"
export REDIS_URL="redis://127.0.0.1:6379/0"
export KAFKA_BOOTSTRAP="127.0.0.1:19092"
export TEMPORAL_ADDRESS="127.0.0.1:7233"
# no object store in the demo: evidence files (dispute packs, call recordings) are not part of the showcase
PUBLIC_URL="https://${SPACE_HOST:-localhost:7860}"
[ -z "${SPACE_HOST:-}" ] && PUBLIC_URL="http://localhost:7860"
export NIRANTAR_PUBLIC_APP_URL="$PUBLIC_URL" CORS_ORIGINS="$PUBLIC_URL"

# ---------------------------------------------------------------- Postgres 16 (+pgvector), RLS roles
printf '%s' "$OWNER_PW" > "$DATA/.pw"
initdb -D "$DATA/pg" -U nirantar_owner --auth-local=scram-sha-256 --auth-host=scram-sha-256 \
  --pwfile="$DATA/.pw" -E UTF8 >/dev/null
rm -f "$DATA/.pw"
pg_ctl -D "$DATA/pg" -l "$DATA/pg.log" -w -o "-c listen_addresses=127.0.0.1 -c port=5432 \
  -k $DATA/pgsock -c max_connections=200 -c shared_buffers=256MB -c fsync=off" start >/dev/null
export PGPASSWORD="$OWNER_PW"
psql -h 127.0.0.1 -U nirantar_owner -d postgres -qc "CREATE DATABASE nirantar"
sed "s/PASSWORD 'nirantar_app'/PASSWORD '${APP_PW}'/" "$SPACE/01-roles.sql" \
  | psql -h 127.0.0.1 -U nirantar_owner -d nirantar -q -v ON_ERROR_STOP=1 >/dev/null
unset PGPASSWORD
log "postgres ready"

# ---------------------------------------------------------------- Redis, Redpanda, object store, Temporal
redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no --loglevel warning &
PIDS+=($!)

cat > "$DATA/redpanda/redpanda.yaml" <<YAML
redpanda:
  data_directory: $DATA/redpanda/data
  developer_mode: true
  auto_create_topics_enabled: true
  kafka_api: [{ address: 127.0.0.1, port: 19092 }]
  advertised_kafka_api: [{ address: 127.0.0.1, port: 19092 }]
  rpc_server: { address: 127.0.0.1, port: 33145 }
  admin: [{ address: 127.0.0.1, port: 9644 }]
YAML
redpanda --redpanda-cfg "$DATA/redpanda/redpanda.yaml" --smp 1 --memory 1G --reserve-memory 0M \
  --overprovisioned --unsafe-bypass-fsync=true --default-log-level=warn > "$DATA/redpanda.log" 2>&1 &
PIDS+=($!)


temporal server start-dev --ip 127.0.0.1 --port 7233 --headless --db-filename "$DATA/temporal.db" \
  --log-level warn > "$DATA/temporal.log" 2>&1 &
PIDS+=($!)

wait_for redis 30 redis-cli -h 127.0.0.1 ping
wait_for redpanda 120 python -c 'from confluent_kafka.admin import AdminClient as A; A({"bootstrap.servers": "127.0.0.1:19092"}).list_topics(timeout=3)'
wait_for temporal 120 temporal operator cluster health --address 127.0.0.1:7233

# ---------------------------------------------------------------- schema + one synthetic business
cd "$APP"
alembic upgrade head > "$DATA/migrate.log" 2>&1 || { cat "$DATA/migrate.log"; exit 1; }
log "migrations applied"
python -m nirantar.demo.seed --customers "${NIRANTAR_DEMO_CUSTOMERS:-60}" > "$DATA/seed.json"
read -r OWNER_KEY APPROVER_KEY TENANT < <(python -c '
import json,sys; d=json.load(open(sys.argv[1])); k=d["api_keys"]
print(k["owner"], k["finance_approver"], d["tenant_id"])' "$DATA/seed.json")
log "demo business seeded ($TENANT)"

# ---------------------------------------------------------------- API, services, web, edge
uvicorn nirantar.api.serve:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 \
  --log-level warning &
PIDS+=($!)
python -m nirantar.services &
PIDS+=($!)
(
  cd /web
  exec env -i PATH="$PATH" HOME="$HOME" NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=127.0.0.1 \
    NIRANTAR_DEMO=1 NIRANTAR_API_URL=http://127.0.0.1:8000 NIRANTAR_APP_URL="$PUBLIC_URL" \
    NIRANTAR_API_KEY="$OWNER_KEY" NIRANTAR_APPROVER_KEY="$APPROVER_KEY" NIRANTAR_PLATFORM_KEY="$PLATFORM_ADMIN_KEY" \
    NIRANTAR_SESSION_SECRET="$(rand)" node server.js
) &
PIDS+=($!)
wait_for api 120 curl -fsS http://127.0.0.1:8000/ready
wait_for web 120 curl -fsS -o /dev/null http://127.0.0.1:3000/
caddy run --config "$SPACE/Caddyfile" --adapter caddyfile &
PIDS+=($!)
log "nirantar demo serving on :7860 ($PUBLIC_URL)"

# Any process exiting ends the container (the platform restarts it from a clean slate).
set +e
wait -n "${PIDS[@]}"
code=$?
log "a process exited (code $code); stopping"
shutdown
exit 1
