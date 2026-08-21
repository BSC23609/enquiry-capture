-- Enquiry Capture — Neon / Postgres schema
-- Run once:  psql "$DATABASE_URL" -f schema.sql

CREATE TABLE IF NOT EXISTS prospects (
    id              BIGSERIAL PRIMARY KEY,

    -- identity / dedup keys
    gstin           CHAR(15),
    email           TEXT,
    mobile          CHAR(10),

    -- the actual data
    company_name    TEXT,
    contact_person  TEXT,
    enquiry_type    TEXT,          -- steel | roofing | peb | other
    city            TEXT,
    state_code      CHAR(2),       -- first 2 digits of GSTIN

    -- provenance
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen      INTEGER     NOT NULL DEFAULT 1,
    last_subject    TEXT,
    from_address    TEXT,
    confidence      TEXT,          -- high | medium | low
    source          TEXT        NOT NULL DEFAULT 'email',

    -- workflow
    status          TEXT        NOT NULL DEFAULT 'new',   -- new | qualified | rejected | promoted
    gst_enriched    BOOLEAN     NOT NULL DEFAULT false,
    notes           TEXT,

    -- human edit protection: any column listed here is never auto-overwritten
    locked_fields   TEXT[]      NOT NULL DEFAULT '{}',

    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Partial unique indexes: NULLs don't collide, so a prospect with only a mobile
-- doesn't block another with only an email.
CREATE UNIQUE INDEX IF NOT EXISTS prospects_gstin_uq
    ON prospects (gstin)  WHERE gstin  IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS prospects_email_uq
    ON prospects (email)  WHERE email  IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS prospects_mobile_uq
    ON prospects (mobile) WHERE mobile IS NOT NULL;

CREATE INDEX IF NOT EXISTS prospects_status_idx    ON prospects (status);
CREATE INDEX IF NOT EXISTS prospects_last_seen_idx ON prospects (last_seen DESC);


-- Every message we looked at, enquiry or not. This is the audit trail that
-- Power Automate's 28-day run history was never going to give you.
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    graph_id        TEXT UNIQUE NOT NULL,      -- idempotency: never process twice
    internet_msg_id TEXT,
    received_at     TIMESTAMPTZ,
    subject         TEXT,
    from_address    TEXT,
    from_name       TEXT,
    body_snippet    TEXT,                      -- cleaned body, first 4000 chars
    verdict         TEXT,                      -- enquiry | junk | no_contact | error
    verdict_reason  TEXT,
    prospect_id     BIGINT REFERENCES prospects(id) ON DELETE SET NULL,
    extracted       JSONB,
    llm_used        BOOLEAN NOT NULL DEFAULT false,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_verdict_idx     ON messages (verdict);
CREATE INDEX IF NOT EXISTS messages_received_idx    ON messages (received_at DESC);
CREATE INDEX IF NOT EXISTS messages_prospect_idx    ON messages (prospect_id);


-- Graph delta cursor, so a restart resumes instead of re-reading the mailbox.
CREATE TABLE IF NOT EXISTS sync_state (
    mailbox         TEXT PRIMARY KEY,
    delta_link      TEXT,
    last_run_at     TIMESTAMPTZ,
    last_error      TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);


-- ============================================================
-- Customer contact book (self-healing)
-- ============================================================
-- Seeded once from Customer_Contact.xlsx, then enriched by inbound mail.
-- Mail from a matched customer fills BLANK fields only; extra numbers/emails
-- are appended to the *_alt arrays rather than overwriting the primary value.

CREATE TABLE IF NOT EXISTS customers (
    id              BIGSERIAL PRIMARY KEY,
    bp_code         TEXT UNIQUE,               -- from the master; NULL for auto-created
    company_name    TEXT,
    contact_person  TEXT,
    email           TEXT,                      -- primary
    mobile          CHAR(10),                  -- primary
    gstin           CHAR(15),
    address         TEXT,
    zip_code        TEXT,
    city            TEXT,

    email_alt       TEXT[]  NOT NULL DEFAULT '{}',   -- additional emails seen
    mobile_alt      TEXT[]  NOT NULL DEFAULT '{}',   -- additional mobiles seen

    source          TEXT    NOT NULL DEFAULT 'seed', -- seed | email
    sender_type     TEXT,                            -- customer (always, for this table)
    times_seen      INTEGER NOT NULL DEFAULT 0,       -- inbound mails matched to this row
    last_contact_at TIMESTAMPTZ,

    -- human-edit protection: listed columns are never auto-touched
    locked_fields   TEXT[]  NOT NULL DEFAULT '{}',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS customers_email_uq
    ON customers (email)  WHERE email  IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS customers_mobile_uq
    ON customers (mobile) WHERE mobile IS NOT NULL;
CREATE INDEX IF NOT EXISTS customers_company_idx ON customers (lower(company_name));
CREATE INDEX IF NOT EXISTS customers_gstin_idx   ON customers (gstin) WHERE gstin IS NOT NULL;


-- Senders classified as NOT a customer. Captured for reference, kept out of
-- the customer book so suppliers/marketing never pollute it.
CREATE TABLE IF NOT EXISTS non_customers (
    id              BIGSERIAL PRIMARY KEY,
    sender_type     TEXT,                      -- supplier | service_provider | marketing | other
    company_name    TEXT,
    contact_person  TEXT,
    email           TEXT,
    mobile          CHAR(10),
    gstin           CHAR(15),
    times_seen      INTEGER NOT NULL DEFAULT 1,
    last_subject    TEXT,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS non_customers_email_uq
    ON non_customers (email)  WHERE email  IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS non_customers_mobile_uq
    ON non_customers (mobile) WHERE mobile IS NOT NULL;


-- Audit of every field an inbound mail filled or added on a customer row.
CREATE TABLE IF NOT EXISTS customer_enrichment_log (
    id            BIGSERIAL PRIMARY KEY,
    customer_id   BIGINT REFERENCES customers(id) ON DELETE CASCADE,
    field         TEXT,                        -- email | mobile | gstin | contact_person | email_alt | mobile_alt ...
    old_value     TEXT,
    new_value     TEXT,
    graph_id      TEXT,                        -- the message that supplied it
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS enrich_log_customer_idx ON customer_enrichment_log (customer_id);
CREATE INDEX IF NOT EXISTS enrich_log_created_idx  ON customer_enrichment_log (created_at DESC);
