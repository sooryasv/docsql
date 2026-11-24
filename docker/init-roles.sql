-- Dev/test/CI bootstrap. Run as a superuser (psql -f). Idempotent.
-- The same statements (with your own password) are in docs/runbook.md for Neon/RDS.
CREATE SCHEMA IF NOT EXISTS analytics;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'docsql_readonly') THEN
    CREATE ROLE docsql_readonly LOGIN PASSWORD 'docsql_readonly';
  END IF;
END
$$;

ALTER ROLE docsql_readonly SET default_transaction_read_only = on;
ALTER ROLE docsql_readonly SET statement_timeout = '3000ms';
ALTER ROLE docsql_readonly SET search_path = analytics;

REVOKE ALL ON SCHEMA public FROM docsql_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM docsql_readonly;
GRANT USAGE ON SCHEMA analytics TO docsql_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO docsql_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO docsql_readonly;

-- Separate database for the test-suite (no-op if it already exists).
SELECT 'CREATE DATABASE docsql_test' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'docsql_test')\gexec
