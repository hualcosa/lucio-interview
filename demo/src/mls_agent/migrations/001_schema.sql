-- Serving schema. Indexed for the predicates users actually ask about, and
-- protected by row-level security keyed to the authenticated principal.
--
-- Two roles on purpose:
--   mls      (owner)  migrations and ingest. Bypasses RLS by design.
--   mls_app           every query path from the domain service. Subject to RLS.
--
-- The separation is the point: application code cannot reach data the principal
-- is not entitled to, even if it forgets to filter. See RESPONSE.md 3.5.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mls_app') THEN
    CREATE ROLE mls_app LOGIN PASSWORD 'mls_app';
  END IF;
END
$$;


CREATE TABLE IF NOT EXISTS listings (
    listing_id      bigint PRIMARY KEY,
    brokered_by     bigint      NOT NULL,
    status          text        NOT NULL,
    price           numeric(12,2),
    bed             smallint,
    bath            smallint,
    acre_lot        numeric(12,3),
    street          bigint,
    city            text,
    state           text,
    zip_code        text,
    house_size      integer,
    prev_sold_date  date,
    list_date       date,
    updated_at      date        NOT NULL,
    -- Digest of the source row. The nightly diff compares this rather than
    -- every column, so "did this listing change?" is one comparison.
    row_hash        bytea       NOT NULL
);

-- Indexes follow the questions, not the columns. Each one exists because a
-- tool in the domain service filters on it.
CREATE INDEX IF NOT EXISTS listings_broker_idx  ON listings (brokered_by);
CREATE INDEX IF NOT EXISTS listings_status_idx  ON listings (status);
CREATE INDEX IF NOT EXISTS listings_price_idx   ON listings (price);
CREATE INDEX IF NOT EXISTS listings_geo_idx     ON listings (state, city);
CREATE INDEX IF NOT EXISTS listings_zip_idx     ON listings (zip_code);
CREATE INDEX IF NOT EXISTS listings_listed_idx  ON listings (list_date);
-- The composite that answers the canonical question: "3-bed under $500k,
-- listed over 90 days, in these places."
CREATE INDEX IF NOT EXISTS listings_search_idx  ON listings (status, bed, price, list_date);
-- Fuzzy name/address matching without embeddings (RESPONSE.md 3.3).
CREATE INDEX IF NOT EXISTS listings_city_trgm   ON listings USING gin (city gin_trgm_ops);


-- Outbox. The legacy system is batch-only, so actions land here rather than
-- travelling back in real time. Consequential ones start life awaiting a human.
CREATE TABLE IF NOT EXISTS actions (
    action_id     bigserial PRIMARY KEY,
    listing_id    bigint      NOT NULL,
    brokered_by   bigint      NOT NULL,
    action_type   text        NOT NULL,
    payload       jsonb       NOT NULL DEFAULT '{}',
    status        text        NOT NULL DEFAULT 'pending_approval'
                  CHECK (status IN ('pending_approval','approved','dispatched','rejected')),
    requested_by  text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS actions_broker_idx ON actions (brokered_by, status);


-- Pipeline observability. These numbers are the evidence for the central
-- economic argument: only the delta is ever processed.
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id           bigserial PRIMARY KEY,
    dump_date        date        NOT NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    records_seen     bigint      NOT NULL DEFAULT 0,
    records_inserted bigint      NOT NULL DEFAULT 0,
    records_updated  bigint      NOT NULL DEFAULT 0,
    records_deleted  bigint      NOT NULL DEFAULT 0,
    status           text        NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','completed','halted')),
    halt_reason      text
);


-- Row-level security.
--
-- The principal is a session variable set from the verified token, never a
-- query parameter. Application code cannot widen its own visibility.
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE actions  ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS listings_tenant_isolation ON listings;
CREATE POLICY listings_tenant_isolation ON listings
    FOR ALL TO mls_app
    USING (brokered_by = current_setting('app.brokerage_id', true)::bigint);

DROP POLICY IF EXISTS actions_tenant_isolation ON actions;
CREATE POLICY actions_tenant_isolation ON actions
    FOR ALL TO mls_app
    USING (brokered_by = current_setting('app.brokerage_id', true)::bigint)
    WITH CHECK (brokered_by = current_setting('app.brokerage_id', true)::bigint);

GRANT SELECT                     ON listings    TO mls_app;
GRANT SELECT, INSERT             ON actions     TO mls_app;
GRANT USAGE, SELECT ON SEQUENCE actions_action_id_seq TO mls_app;

-- Note: `current_setting(..., true)` returns NULL when unset, and NULL = bigint
-- is never true, so a session that forgets to establish a principal sees
-- nothing. Failing closed is the only acceptable default here.
