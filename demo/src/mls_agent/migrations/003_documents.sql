-- Documents and their embedded passages.
--
-- Documents live in their own tables rather than as extracted columns on
-- `listings`, and that is a deliberate answer to an obvious objection: why not
-- just store `allows_short_term_rental` as a boolean?
--
--   1. A listing with no documents has NO ROWS here. As a column it would be
--      NULL, and NULL cannot distinguish "no document" from "document says no".
--   2. The clauses are conditional prose -- "prohibited under 30 days, except
--      for units acquired before 2019, subject to Board approval". No boolean
--      survives that.
--   3. An agent advising a client must cite the paragraph, not a flag.
--
-- See RESPONSE.md 3.3.

CREATE EXTENSION IF NOT EXISTS vector;


CREATE TABLE IF NOT EXISTS documents (
    document_id   bigserial PRIMARY KEY,
    listing_id    bigint  NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    brokered_by   bigint  NOT NULL,
    doc_type      text    NOT NULL,
    title         text    NOT NULL,
    body          text    NOT NULL,
    -- Digest of `body`. An unchanged document is not re-chunked and not
    -- re-embedded, which is the whole economic argument (RESPONSE.md 3.4).
    content_hash  bytea   NOT NULL,
    updated_at    date    NOT NULL,
    UNIQUE (listing_id, doc_type)
);

CREATE INDEX IF NOT EXISTS documents_listing_idx ON documents (listing_id);
CREATE INDEX IF NOT EXISTS documents_broker_idx  ON documents (brokered_by);


CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     bigserial PRIMARY KEY,
    document_id  bigint      NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    -- listing_id and brokered_by are denormalised on purpose. The hybrid query
    -- joins chunks straight to listings, and the RLS policy must test tenancy on
    -- the chunk itself -- a policy that has to join in order to decide is
    -- evaluated per row and is dramatically slower.
    listing_id   bigint      NOT NULL,
    brokered_by  bigint      NOT NULL,
    ordinal      int         NOT NULL,
    passage      text        NOT NULL,
    -- halfvec: 2 bytes per dimension instead of 4. Halves the index, and HNSW
    -- wants the index resident in RAM, which on Aurora is ACUs, which is money.
    -- Recall loss on normalised embeddings is negligible.
    embedding    halfvec(256),
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS chunks_listing_idx ON chunks (listing_id);

-- HNSW: a layered navigable graph. Top layer holds a few well-connected hubs
-- with long links; each layer below is denser with shorter ones. A search starts
-- at the top, hops toward the target, drops a layer, refines. Visits a few
-- hundred vectors instead of all of them -- approximate, tuned by ef_search.
--
-- Cosine because Titan V2 embeddings are normalised.
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding halfvec_cosine_ops);


-- Row-level security, same principal as everything else.
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks    ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS documents_tenant_isolation ON documents;
CREATE POLICY documents_tenant_isolation ON documents
    FOR ALL TO mls_app
    USING (brokered_by = current_setting('app.brokerage_id', true)::bigint);

DROP POLICY IF EXISTS chunks_tenant_isolation ON chunks;
CREATE POLICY chunks_tenant_isolation ON chunks
    FOR ALL TO mls_app
    USING (brokered_by = current_setting('app.brokerage_id', true)::bigint);

GRANT SELECT ON documents TO mls_app;
GRANT SELECT ON chunks    TO mls_app;


-- iterative_scan is not optional.
--
-- Without it, an HNSW search that also has a WHERE clause returns whatever
-- survives the filter out of the first ef_search candidates -- which for a
-- selective filter can be a small fraction of the true matches, with no error
-- and no warning. Silent partial results are the worst possible failure mode
-- for a compliance-sensitive client.
--
-- Set at database level so no code path can forget it.
ALTER DATABASE mls SET hnsw.iterative_scan = 'relaxed_order';
