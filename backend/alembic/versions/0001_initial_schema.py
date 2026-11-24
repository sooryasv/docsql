"""Initial schema: public (app + RAG), analytics (read-only SQL target), role grants.

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE SCHEMA IF NOT EXISTS analytics",
    """CREATE TABLE public.users (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      email VARCHAR(255) NOT NULL UNIQUE,
      full_name VARCHAR(120) NOT NULL,
      password_hash VARCHAR(255) NOT NULL,
      role VARCHAR(20) NOT NULL CHECK (role IN ('ANALYST','ADMIN')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      CONSTRAINT ck_users_email_lowercase CHECK (email = LOWER(email)))""",
    """CREATE TABLE public.documents (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      uploaded_by UUID REFERENCES public.users(id) ON DELETE CASCADE,
      title VARCHAR(200) NOT NULL,
      filename VARCHAR(255) NOT NULL,
      file_sha256 CHAR(64) NOT NULL,
      page_count INTEGER NOT NULL CHECK (page_count > 0 AND page_count <= 15),
      chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
      embedding_model VARCHAR(80) NOT NULL,
      is_system_seed BOOLEAN NOT NULL DEFAULT FALSE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      CONSTRAINT ck_documents_owner_or_seed CHECK (is_system_seed OR uploaded_by IS NOT NULL))""",
    "CREATE INDEX ix_documents_owner_seed ON public.documents (is_system_seed, uploaded_by, created_at DESC)",
    "CREATE UNIQUE INDEX uq_documents_seed_filename ON public.documents (filename) WHERE is_system_seed",
    "CREATE UNIQUE INDEX uq_documents_owner_sha ON public.documents (uploaded_by, file_sha256) WHERE uploaded_by IS NOT NULL",
    """CREATE TABLE public.document_chunks (
      id BIGSERIAL PRIMARY KEY,
      document_id UUID NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
      page_number INTEGER NOT NULL CHECK (page_number > 0),
      chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
      content TEXT NOT NULL,
      embedding vector(768) NOT NULL,
      search_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
      CONSTRAINT uq_chunks_doc_index UNIQUE (document_id, chunk_index))""",
    "CREATE INDEX ix_chunks_doc_page ON public.document_chunks (document_id, page_number)",
    "CREATE INDEX ix_chunks_tsv_gin ON public.document_chunks USING GIN (search_tsv)",
    """CREATE INDEX ix_chunks_embedding_hnsw ON public.document_chunks
      USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)""",
    """CREATE TABLE public.query_logs (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
      question TEXT NOT NULL,
      route VARCHAR(20) CHECK (route IN ('DOC','SQL','HYBRID','OUT_OF_SCOPE')),
      generated_sql TEXT,
      sql_blocked BOOLEAN NOT NULL DEFAULT FALSE,
      sql_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (sql_retry_count BETWEEN 0 AND 1),
      refused BOOLEAN NOT NULL DEFAULT FALSE,
      error_code VARCHAR(40),
      cited_chunk_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
      chart_spec JSONB,
      answer_text TEXT NOT NULL DEFAULT '',
      latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
      tokens_used INTEGER NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    "CREATE INDEX ix_query_logs_user_created ON public.query_logs (user_id, created_at DESC)",
    "CREATE INDEX ix_query_logs_created_route ON public.query_logs (created_at DESC, route)",
    """CREATE TABLE public.usage_counters (
      scope VARCHAR(80) NOT NULL,
      usage_date DATE NOT NULL,
      used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
      PRIMARY KEY (scope, usage_date))""",
    """CREATE TABLE analytics.regions (
      id SERIAL PRIMARY KEY, region_code VARCHAR(16) NOT NULL UNIQUE,
      city VARCHAR(80) NOT NULL, state VARCHAR(80) NOT NULL,
      zone VARCHAR(32) NOT NULL CHECK (zone IN ('SOUTH','WEST','NORTH','EAST')),
      q3_target_inr BIGINT NOT NULL CHECK (q3_target_inr > 0))""",
    """CREATE TABLE analytics.products (
      id SERIAL PRIMARY KEY, sku VARCHAR(32) NOT NULL UNIQUE, name VARCHAR(140) NOT NULL,
      category VARCHAR(60) NOT NULL
        CHECK (category IN ('CLOUD_OPS','DATA_PLATFORM','SECURITY','AI_SUITE')),
      unit_price_inr INTEGER NOT NULL CHECK (unit_price_inr > 0),
      max_discount_pct INTEGER NOT NULL CHECK (max_discount_pct BETWEEN 0 AND 50))""",
    """CREATE TABLE analytics.orders (
      id SERIAL PRIMARY KEY, order_code VARCHAR(32) NOT NULL UNIQUE,
      region_id INTEGER NOT NULL REFERENCES analytics.regions(id) ON DELETE RESTRICT,
      product_id INTEGER NOT NULL REFERENCES analytics.products(id) ON DELETE RESTRICT,
      order_date DATE NOT NULL,
      quarter VARCHAR(10) NOT NULL CHECK (quarter IN ('2026-Q1','2026-Q2','2026-Q3','2026-Q4')),
      quantity INTEGER NOT NULL CHECK (quantity > 0),
      discount_pct INTEGER NOT NULL CHECK (discount_pct BETWEEN 0 AND 50),
      revenue_inr BIGINT NOT NULL CHECK (revenue_inr > 0),
      customer_tier VARCHAR(20) NOT NULL
        CHECK (customer_tier IN ('STARTUP','MID_MARKET','ENTERPRISE')))""",
    "CREATE INDEX ix_orders_quarter_region ON analytics.orders (quarter, region_id)",
    "CREATE INDEX ix_orders_product_date ON analytics.orders (product_id, order_date)",
    # Grants only if the read-only role exists (it is created by docker/init-roles.sql or runbook).
    """DO $$
    BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'docsql_readonly') THEN
        REVOKE ALL ON SCHEMA public FROM docsql_readonly;
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM docsql_readonly;
        GRANT USAGE ON SCHEMA analytics TO docsql_readonly;
        GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO docsql_readonly;
        ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO docsql_readonly;
      END IF;
    END
    $$""",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS analytics CASCADE")
    for table in ("usage_counters", "query_logs", "document_chunks", "documents", "users"):
        op.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")
