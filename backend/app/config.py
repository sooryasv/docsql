"""Typed settings. Every tunable comes from the environment; no secrets live in code."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_DIM = 768


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "postgresql://postgres:postgres@localhost:5432/docsql"
    analytics_readonly_database_url: str = (
        "postgresql://docsql_readonly:docsql_readonly@localhost:5432/docsql"
    )
    use_null_pool: bool = False

    jwt_secret: str = Field(default="dev-only-secret-change-me-dev-only-secret", min_length=16)
    jwt_expire_minutes: int = 60
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:5173,http://localhost:8000"
    enable_demo_tools: bool = True

    run_seed_on_startup: bool = False
    frontend_dist_dir: str = ""

    mock_llm: bool = True
    gemini_api_key: str = ""
    gemini_chat_model: str = "gemini-2.5-flash-lite"
    gemini_embed_model: str = "gemini-embedding-001"
    min_cosine_similarity: float = 0.35

    daily_query_limit: int = 20
    global_daily_query_limit: int = 200
    max_docs_per_user: int = 5
    sql_timeout_ms: int = 3000
    sql_max_rows: int = 100
    llm_timeout_s: float = 20
    embed_timeout_s: float = 15
    request_deadline_s: float = 45
    sse_ping_interval_s: float = 10
    pdf_parse_timeout_s: float = 10

    @property
    def origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
