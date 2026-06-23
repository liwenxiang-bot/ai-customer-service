-- Ensure required extensions exist as soon as the database is created.
-- (Alembic also creates these idempotently, for non-docker deployments.)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
