-- Owner role (nirantar_owner) runs migrations. The application connects as
-- nirantar_app, which is NOT the table owner and does NOT bypass RLS, so every
-- tenant-scoped query is filtered by the database itself (BB-§7).
CREATE ROLE nirantar_app LOGIN PASSWORD 'nirantar_app' NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE nirantar TO nirantar_app;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE DATABASE mlflow OWNER nirantar_owner;
