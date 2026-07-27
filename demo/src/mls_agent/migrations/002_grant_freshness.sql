-- The domain service reports data currency on every response (RESPONSE.md 3.7),
-- which means the application role needs to read when the last load completed.
--
-- Read-only, and no RLS policy: freshness is not tenant-scoped. Every principal
-- is entitled to know how stale the answer they just received is.

GRANT SELECT ON ingest_runs TO mls_app;
