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
