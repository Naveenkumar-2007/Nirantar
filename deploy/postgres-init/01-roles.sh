#!/bin/sh
# Production roles (first init only). nirantar_app is NOT the table owner and does NOT bypass RLS (BB-§7).
set -e
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"
: "${KEYCLOAK_DB_PASSWORD:?KEYCLOAK_DB_PASSWORD is required}"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE nirantar_app LOGIN PASSWORD '${APP_DB_PASSWORD}' NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE nirantar TO nirantar_app;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE DATABASE mlflow OWNER nirantar_owner;
CREATE DATABASE nirantar_lake OWNER nirantar_owner;
CREATE ROLE keycloak LOGIN PASSWORD '${KEYCLOAK_DB_PASSWORD}';
CREATE DATABASE keycloak OWNER keycloak;
SQL
