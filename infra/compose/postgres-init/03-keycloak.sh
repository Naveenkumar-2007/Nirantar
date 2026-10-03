#!/bin/sh
# Keycloak's own database (identity data stays out of the product schema). Runs on first init only;
# existing installs: `docker compose exec postgres sh /docker-entrypoint-initdb.d/03-keycloak.sh`.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres <<SQL
SELECT 'CREATE ROLE keycloak LOGIN PASSWORD ''${KEYCLOAK_DB_PASSWORD:-keycloak}'''
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'keycloak')\gexec
SELECT 'CREATE DATABASE keycloak OWNER keycloak'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec
SQL
