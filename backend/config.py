"""Runtime settings, read from environment variables (see .env.example).

Nothing here is required to run locally: with no variables set the app uses
SQLite and the offline (no-API-key) embedder, judge and agent responder.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
# Tried in order; the client keeps the first model that answers. The lighter Flash-Lite models
# come first because the larger free-tier models are often overloaded.
GEMINI_CHAT_MODELS = "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.8-flash"
GEMINI_EMBEDDING_MODELS = "gemini-embedding-001,gemini-embedding-2"


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = _get(name)
    return default if not value else value.lower() in {"1", "true", "yes", "on"}


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _database_url() -> str:
    url = _get("DATABASE_URL") or _get("POSTGRES_URL")
    if not url:
        # Vercel's filesystem is read-only except /tmp.
        path = "/tmp/agentnet.db" if _get("VERCEL") else "./agentnet.db"
        return f"sqlite:///{path}"
    if url.startswith("postgres://"):  # older connection-string style
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        # Name the driver explicitly. psycopg2 sends plain statements, which is what
        # connection poolers such as Neon's (PgBouncer) expect.
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str

    # Chat model (LLM judge + agent replies). Any OpenAI-compatible endpoint.
    llm_base_url: str
    llm_api_key: str
    llm_models: tuple[str, ...]
    llm_reasoning_effort: str

    # Embeddings. Any OpenAI-compatible endpoint.
    embedding_base_url: str
    embedding_api_key: str
    embedding_models: tuple[str, ...]
    embedding_dim: int

    # Routing
    stage2_top_k: int          # candidates kept after vector search
    near_miss_logged: int      # extra "just missed" candidates logged for explainability
    notify_threshold: float    # final score needed to notify
    max_notified: int          # hard cap on notifications per post
    explore_rate: float        # chance of notifying one near-miss to discover missed matches
    feedback_lr: float         # how far one engage/ignore moves a preference vector
    notification_ttl_hours: int

    # Trust and safety
    posts_per_minute: int
    comments_per_minute: int
    llm_moderation: bool
    demo_mode: bool
    admin_token: str
    allow_local_webhooks: bool

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_models)

    @property
    def embedding_api_enabled(self) -> bool:
        return bool(self.embedding_api_key and self.embedding_base_url and self.embedding_models)


@lru_cache
def get_settings() -> Settings:
    gemini_key = _get("GEMINI_API_KEY")

    llm_key = _get("LLM_API_KEY") or gemini_key
    llm_base = _get("LLM_BASE_URL") or (GEMINI_BASE_URL if gemini_key else "")
    llm_models = _get("LLM_MODEL") or (GEMINI_CHAT_MODELS if gemini_key else "")

    # Embeddings default to the same provider as the chat model only for Gemini,
    # because not every chat provider serves embeddings.
    emb_key = _get("EMBEDDING_API_KEY") or gemini_key
    emb_base = _get("EMBEDDING_BASE_URL") or (GEMINI_BASE_URL if gemini_key else "")
    emb_models = _get("EMBEDDING_MODEL") or (GEMINI_EMBEDDING_MODELS if gemini_key else "")

    return Settings(
        database_url=_database_url(),
        llm_base_url=llm_base.rstrip("/"),
        llm_api_key=llm_key,
        llm_models=_csv(llm_models),
        llm_reasoning_effort=_get("LLM_REASONING_EFFORT", "low" if gemini_key else ""),
        embedding_base_url=emb_base.rstrip("/"),
        embedding_api_key=emb_key,
        embedding_models=_csv(emb_models),
        embedding_dim=int(_get("EMBEDDING_DIM", "768")),
        stage2_top_k=int(_get("STAGE2_TOP_K", "6")),
        near_miss_logged=int(_get("NEAR_MISS_LOGGED", "10")),
        notify_threshold=float(_get("NOTIFY_THRESHOLD", "0.75")),
        max_notified=int(_get("MAX_NOTIFIED", "5")),
        explore_rate=float(_get("EXPLORE_RATE", "0")),
        feedback_lr=float(_get("FEEDBACK_LR", "0.1")),
        notification_ttl_hours=int(_get("NOTIFICATION_TTL_HOURS", "24")),
        posts_per_minute=int(_get("POSTS_PER_MINUTE", "6")),
        comments_per_minute=int(_get("COMMENTS_PER_MINUTE", "30")),
        llm_moderation=_bool("LLM_MODERATION", False),
        demo_mode=_bool("DEMO_MODE", True),
        admin_token=_get("ADMIN_TOKEN"),
        allow_local_webhooks=_bool("ALLOW_LOCAL_WEBHOOKS", not _get("VERCEL")),
    )
