"""Initial schema: domains, RLS tenant isolation, immutability triggers.

Revision ID: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = r"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS events;
CREATE SCHEMA IF NOT EXISTS ledger;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS experiments;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS ai;

-- ---------------------------------------------------------------- core
CREATE TABLE core.tenants (
  tenant_id     text PRIMARY KEY,
  name          text NOT NULL,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','closed')),
  settings      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.provider_accounts (
  tenant_id     text NOT NULL REFERENCES core.tenants(tenant_id),
  provider      text NOT NULL CHECK (provider IN ('razorpay','cashfree','stripe','mock')),
  mode          text NOT NULL CHECK (mode IN ('test','live')),
  account_ref   text,
  secret_ref    text NOT NULL,           -- reference into the secret store, never the secret
  capabilities  jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, provider, mode)
);

CREATE TABLE core.idempotency_keys (
  tenant_id     text NOT NULL,
  scope         text NOT NULL,
  key           text NOT NULL,
  request_hash  text NOT NULL,
  response      jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scope, key)
);

-- ---------------------------------------------------------------- billing
CREATE TABLE billing.customers (
  tenant_id          text NOT NULL REFERENCES core.tenants(tenant_id),
  customer_id        text NOT NULL,
  external_ref       text,
  display_name       text,
  phone_enc          bytea,              -- AES-GCM envelope (core.crypto); never plaintext
  email_enc          bytea,
  contact_hash       text,               -- keyed hash for lookups without decrypting
  preferred_language text NOT NULL DEFAULT 'en',
  timezone           text NOT NULL DEFAULT 'Asia/Kolkata',
  consents           jsonb NOT NULL DEFAULT '{}'::jsonb,
  segment            text,               -- product line: subscription|lending|sip|insurance|b2b
  created_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, customer_id),
  UNIQUE (tenant_id, external_ref)
);

CREATE TABLE billing.mandates (
  tenant_id          text NOT NULL,
  mandate_id         text NOT NULL,
  customer_id        text NOT NULL,
  provider           text NOT NULL,
  provider_token_id  text,
  rail               text NOT NULL CHECK (rail IN ('upi_autopay','emandate','card','nach','other')),
  max_amount_minor   bigint NOT NULL CHECK (max_amount_minor > 0),
  currency           text NOT NULL DEFAULT 'INR',
  status             text NOT NULL CHECK (status IN ('pending','active','paused','revoked','expired','failed')),
  valid_until        date,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, mandate_id),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES billing.customers(tenant_id, customer_id)
);

CREATE TABLE billing.subscriptions (
  tenant_id                 text NOT NULL,
  subscription_id           text NOT NULL,
  customer_id               text NOT NULL,
  mandate_id                text,
  provider                  text NOT NULL,
  provider_subscription_id  text,
  amount_minor              bigint NOT NULL CHECK (amount_minor > 0),
  currency                  text NOT NULL DEFAULT 'INR',
  interval                  text NOT NULL CHECK (interval IN ('weekly','monthly','quarterly','yearly')),
  status                    text NOT NULL CHECK (status IN ('created','active','paused','halted','cancelled','completed')),
  next_charge_on            date,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, subscription_id),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES billing.customers(tenant_id, customer_id),
  UNIQUE (tenant_id, provider, provider_subscription_id)
);

CREATE TABLE billing.debits (
  tenant_id            text NOT NULL,
  debit_id             text NOT NULL,
  subscription_id      text NOT NULL,
  customer_id          text NOT NULL,
  scheduled_for        date NOT NULL,
  amount_minor         bigint NOT NULL CHECK (amount_minor > 0),
  currency             text NOT NULL DEFAULT 'INR',
  status               text NOT NULL CHECK (status IN ('scheduled','notified','attempting','succeeded','failed','cancelled')),
  predebit_notified_at timestamptz,
  attempt_count        int NOT NULL DEFAULT 0,
  provider_payment_id  text,
  last_error_code      text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, debit_id),
  FOREIGN KEY (tenant_id, subscription_id) REFERENCES billing.subscriptions(tenant_id, subscription_id)
);
CREATE INDEX debits_due ON billing.debits (tenant_id, scheduled_for, status);

CREATE TABLE billing.payments (
  tenant_id            text NOT NULL,
  payment_id           text NOT NULL,
  provider             text NOT NULL,
  provider_payment_id  text NOT NULL,
  debit_id             text,
  customer_id          text,
  amount_minor         bigint NOT NULL CHECK (amount_minor >= 0),
  currency             text NOT NULL DEFAULT 'INR',
  status               text NOT NULL CHECK (status IN ('created','authorized','captured','failed','refunded','partially_refunded')),
  method               text,
  error_code           text,
  error_reason         text,
  provider_created_at  timestamptz,
  raw_event_id         text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, payment_id),
  UNIQUE (tenant_id, provider, provider_payment_id)
);

-- ---------------------------------------------------------------- ingest (raw provider events, never lost)
CREATE TABLE ingest.provider_events (
  tenant_id          text NOT NULL,
  raw_event_id       text NOT NULL,
  provider           text NOT NULL,
  provider_event_id  text NOT NULL,
  event_type         text NOT NULL,
  signature_valid    boolean NOT NULL,
  received_at        timestamptz NOT NULL DEFAULT now(),
  headers            jsonb NOT NULL DEFAULT '{}'::jsonb,   -- redacted
  body               bytea NOT NULL,                        -- exact raw bytes
  body_sha256        text NOT NULL,
  status             text NOT NULL DEFAULT 'received' CHECK (status IN ('received','processed','failed','dead','rejected')),
  attempts           int NOT NULL DEFAULT 0,
  last_error         text,
  PRIMARY KEY (tenant_id, raw_event_id),
  UNIQUE (tenant_id, provider, provider_event_id)
);

-- ---------------------------------------------------------------- events (transactional outbox)
CREATE TABLE events.outbox (
  tenant_id     text NOT NULL,
  event_id      text NOT NULL,
  event_type    text NOT NULL,
  version       int NOT NULL,
  subject_id    text NOT NULL,
  envelope      jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz,
  PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX outbox_unpublished ON events.outbox (created_at) WHERE published_at IS NULL;

-- ---------------------------------------------------------------- ledger (immutable)
CREATE TABLE ledger.accounts (
  tenant_id  text NOT NULL,
  code       text NOT NULL,
  type       text NOT NULL CHECK (type IN ('asset','liability','equity','income','expense')),
  currency   text NOT NULL DEFAULT 'INR',
  PRIMARY KEY (tenant_id, code)
);

CREATE TABLE ledger.entries (
  tenant_id        text NOT NULL,
  entry_id         text NOT NULL,
  idempotency_key  text NOT NULL,
  memo             text NOT NULL,
  effective_at     timestamptz NOT NULL,
  posted_at        timestamptz NOT NULL,
  provider_ref     text,
  reverses         text,
  body_hash        text NOT NULL,
  PRIMARY KEY (tenant_id, entry_id),
  UNIQUE (tenant_id, idempotency_key),
  UNIQUE (tenant_id, reverses)
);

CREATE TABLE ledger.lines (
  tenant_id     text NOT NULL,
  entry_id      text NOT NULL,
  line_no       int NOT NULL,
  account_code  text NOT NULL,
  side          char(1) NOT NULL CHECK (side IN ('D','C')),
  amount_minor  bigint NOT NULL CHECK (amount_minor > 0),
  currency      text NOT NULL,
  PRIMARY KEY (tenant_id, entry_id, line_no),
  FOREIGN KEY (tenant_id, entry_id) REFERENCES ledger.entries(tenant_id, entry_id),
  FOREIGN KEY (tenant_id, account_code) REFERENCES ledger.accounts(tenant_id, code)
);

-- ---------------------------------------------------------------- audit (hash chain, immutable)
CREATE TABLE audit.records (
  tenant_id  text NOT NULL,
  seq        bigint NOT NULL,
  at         timestamptz NOT NULL,
  actor      text NOT NULL,
  action     text NOT NULL,
  data_hash  text NOT NULL,
  prev_hash  text NOT NULL,
  hash       text NOT NULL,
  detail     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- redacted, no raw PII
  PRIMARY KEY (tenant_id, seq)
);

-- ---------------------------------------------------------------- experiments
CREATE TABLE experiments.experiments (
  tenant_id      text NOT NULL,
  experiment_id  text NOT NULL,
  name           text NOT NULL,
  holdout_bp     int NOT NULL CHECK (holdout_bp BETWEEN 0 AND 10000),  -- basis points
  arms           jsonb NOT NULL,
  status         text NOT NULL DEFAULT 'running' CHECK (status IN ('draft','running','stopped')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, experiment_id)
);

CREATE TABLE experiments.assignments (
  tenant_id      text NOT NULL,
  experiment_id  text NOT NULL,
  customer_id    text NOT NULL,
  arm            text NOT NULL,
  assigned_at    timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, experiment_id, customer_id)
);

CREATE TABLE experiments.exposures (
  tenant_id      text NOT NULL,
  exposure_id    text NOT NULL,
  experiment_id  text NOT NULL,
  customer_id    text NOT NULL,
  arm            text NOT NULL,
  action_ref     text,
  exposed_at     timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, exposure_id)
);

CREATE TABLE experiments.outcomes (
  tenant_id      text NOT NULL,
  outcome_id     text NOT NULL,
  experiment_id  text NOT NULL,
  customer_id    text NOT NULL,
  debit_id       text,
  outcome        text NOT NULL,
  value_minor    bigint NOT NULL DEFAULT 0,
  verified       boolean NOT NULL,
  observed_at    timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, outcome_id)
);

-- ---------------------------------------------------------------- ops (cases, actions, approvals, contact log)
CREATE TABLE ops.cases (
  tenant_id    text NOT NULL,
  case_id      text NOT NULL,
  kind         text NOT NULL CHECK (kind IN ('debit_cycle','collections','dispute','revival','mandate')),
  subject_id   text NOT NULL,
  customer_id  text NOT NULL,
  status       text NOT NULL CHECK (status IN ('open','waiting','escalated','resolved','closed')),
  workflow_id  text,
  summary      jsonb NOT NULL DEFAULT '{}'::jsonb,
  opened_at    timestamptz NOT NULL DEFAULT now(),
  closed_at    timestamptz,
  PRIMARY KEY (tenant_id, case_id)
);

CREATE TABLE ops.actions (
  tenant_id            text NOT NULL,
  action_id            text NOT NULL,
  case_id              text,
  agent_id             text NOT NULL,
  tool_name            text NOT NULL,
  params               jsonb NOT NULL,
  params_hash          text NOT NULL,
  idempotency_key      text NOT NULL,
  policy_decision      text CHECK (policy_decision IN ('ALLOW','DENY','REQUIRE_APPROVAL','REQUIRE_MORE_INFORMATION')),
  policy_version       text,
  approval_id          text,
  status               text NOT NULL CHECK (status IN ('proposed','denied','pending_approval','executing','executed','failed','verified','unverified')),
  provider_request_id  text,
  result               jsonb,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, action_id),
  UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE ops.approvals (
  tenant_id     text NOT NULL,
  approval_id   text NOT NULL,
  action_id     text NOT NULL,
  reason        text NOT NULL,
  status        text NOT NULL CHECK (status IN ('pending','granted','denied','expired')),
  requested_at  timestamptz NOT NULL DEFAULT now(),
  decided_by    text,
  decided_at    timestamptz,
  token_jti     text,
  expires_at    timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, approval_id)
);

CREATE TABLE ops.contacts (
  tenant_id    text NOT NULL,
  contact_id   text NOT NULL,
  customer_id  text NOT NULL,
  channel      text NOT NULL CHECK (channel IN ('whatsapp','sms','email','voice')),
  purpose      text NOT NULL,
  action_id    text,
  status       text NOT NULL,
  at           timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, contact_id)
);
CREATE INDEX contacts_recent ON ops.contacts (tenant_id, customer_id, at DESC);

-- ---------------------------------------------------------------- ai (predictions, labels, memory, RAG, evidence)
CREATE TABLE ai.predictions (
  tenant_id      text NOT NULL,
  prediction_id  text NOT NULL,
  model_name     text NOT NULL,
  model_version  text NOT NULL,
  subject_id     text NOT NULL,
  features_hash  text NOT NULL,
  score          double precision,
  output         jsonb NOT NULL,
  predicted_at   timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, prediction_id)
);

CREATE TABLE ai.labels (
  tenant_id      text NOT NULL,
  label_id       text NOT NULL,
  prediction_id  text,
  subject_id     text NOT NULL,
  label_name     text NOT NULL,
  value          jsonb NOT NULL,
  source         text NOT NULL,       -- 'verifier', 'provider', 'human'
  observed_at    timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, label_id)
);

CREATE TABLE ai.memory (
  tenant_id      text NOT NULL,
  memory_id      text NOT NULL,
  scope          text NOT NULL CHECK (scope IN ('customer','merchant','procedural','episodic')),
  subject_id     text NOT NULL,
  key            text NOT NULL,
  value          jsonb NOT NULL,
  source         text NOT NULL,
  confidence     real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  provenance     jsonb NOT NULL,
  consent_basis  text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  superseded_by  text,
  PRIMARY KEY (tenant_id, memory_id)
);

CREATE TABLE ai.documents (
  tenant_id      text NOT NULL,     -- 'ten_global' for shared regulatory corpus
  doc_id         text NOT NULL,
  title          text NOT NULL,
  document_type  text NOT NULL,
  regulation     text,
  jurisdiction   text NOT NULL DEFAULT 'IN',
  rail           text,
  effective_date date,
  version        text NOT NULL,
  source_uri     text NOT NULL,
  sha256         text NOT NULL,
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, doc_id)
);

CREATE TABLE ai.chunks (
  tenant_id     text NOT NULL,
  chunk_id      text NOT NULL,
  doc_id        text NOT NULL,
  section_path  text NOT NULL,
  text          text NOT NULL,
  tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding     vector(384),
  PRIMARY KEY (tenant_id, chunk_id),
  FOREIGN KEY (tenant_id, doc_id) REFERENCES ai.documents(tenant_id, doc_id)
);
CREATE INDEX chunks_tsv ON ai.chunks USING gin (tsv);
CREATE INDEX chunks_embedding ON ai.chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE ai.evidence (
  tenant_id         text NOT NULL,
  evidence_id       text NOT NULL,
  case_id           text,
  customer_id       text,
  modality          text NOT NULL CHECK (modality IN ('audio','text','voice_note','screenshot','pdf','document','aa_json','email')),
  source_uri        text NOT NULL,
  extracted_fields  jsonb NOT NULL DEFAULT '{}'::jsonb,
  language          text,
  confidence        real,
  verified_against  text,
  hash              text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, evidence_id)
);

-- ---------------------------------------------------------------- immutability
CREATE FUNCTION core.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'table %.% is append-only', TG_TABLE_SCHEMA, TG_TABLE_NAME;
END $$;

CREATE TRIGGER entries_append_only BEFORE UPDATE OR DELETE ON ledger.entries
  FOR EACH ROW EXECUTE FUNCTION core.reject_mutation();
CREATE TRIGGER lines_append_only BEFORE UPDATE OR DELETE ON ledger.lines
  FOR EACH ROW EXECUTE FUNCTION core.reject_mutation();
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit.records
  FOR EACH ROW EXECUTE FUNCTION core.reject_mutation();
CREATE TRIGGER raw_event_body_immutable BEFORE DELETE ON ingest.provider_events
  FOR EACH ROW EXECUTE FUNCTION core.reject_mutation();
"""

# Tables whose rows belong to exactly one tenant.
TENANT_TABLES = [
    "core.tenants", "core.provider_accounts", "core.idempotency_keys",
    "billing.customers", "billing.mandates", "billing.subscriptions", "billing.debits", "billing.payments",
    "ingest.provider_events", "events.outbox",
    "ledger.accounts", "ledger.entries", "ledger.lines", "audit.records",
    "experiments.experiments", "experiments.assignments", "experiments.exposures", "experiments.outcomes",
    "ops.cases", "ops.actions", "ops.approvals", "ops.contacts",
    "ai.predictions", "ai.labels", "ai.memory", "ai.evidence",
]
# Tables that also expose the shared 'ten_global' corpus read-only.
SHARED_READ_TABLES = ["ai.documents", "ai.chunks"]
APPEND_ONLY = {"ledger.entries", "ledger.lines", "audit.records"}

CURRENT = "current_setting('app.tenant_id', true)"


def _rls_sql() -> str:
    out = []
    for table in TENANT_TABLES:
        out.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        out.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        out.append(
            f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {CURRENT}) "
            f"WITH CHECK (tenant_id = {CURRENT});"
        )
        grants = "SELECT, INSERT" if table in APPEND_ONLY else "SELECT, INSERT, UPDATE"
        out.append(f"GRANT {grants} ON {table} TO nirantar_app;")
    for table in SHARED_READ_TABLES:
        out.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        out.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        out.append(
            f"CREATE POLICY tenant_or_global_read ON {table} FOR SELECT "
            f"USING (tenant_id = {CURRENT} OR tenant_id = 'ten_global');"
        )
        out.append(
            f"CREATE POLICY tenant_write ON {table} FOR INSERT WITH CHECK (tenant_id = {CURRENT});"
        )
        out.append(f"GRANT SELECT, INSERT ON {table} TO nirantar_app;")
    schemas = "core, billing, ingest, events, ledger, audit, experiments, ops, ai"
    out.append(f"GRANT USAGE ON SCHEMA {schemas} TO nirantar_app;")
    out.append("GRANT DELETE ON core.idempotency_keys TO nirantar_app;")
    return "\n".join(out)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(_rls_sql())


def downgrade() -> None:
    op.execute(
        "DROP SCHEMA IF EXISTS ai, ops, experiments, audit, ledger, events, ingest, billing, core CASCADE;"
    )
