"""Database engine, session and the portable embedding column type.

Production: PostgreSQL + pgvector (a real `vector` column, searched in SQL).
Local dev / tests: SQLite, where the same column is stored as JSON and the
vector search runs in Python. Only `routing.stage2_vector_search` cares.
"""
from __future__ import annotations

import logging
from typing import Iterator

from sqlalchemy import JSON, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from .config import get_settings

log = logging.getLogger("agentnet.db")
settings = get_settings()


def _make_engine(url: str):
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(eng, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover - tiny hook
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        return eng
    # Small pool: on serverless every warm instance holds its own connections,
    # so use the provider's pooled connection string (Neon "-pooler" host).
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=3, pool_recycle=280)


engine = _make_engine(settings.database_url)
IS_POSTGRES = engine.dialect.name == "postgresql"
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class EmbeddingType(TypeDecorator):
    """pgvector `vector(dim)` on PostgreSQL, JSON array everywhere else."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(settings.embedding_dim))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        return None if value is None else [float(x) for x in value]

    def process_result_value(self, value, dialect):
        return None if value is None else [float(x) for x in value]


_initialised = False


def init_db(seed: bool = True) -> None:
    """Create extension, tables and index; seed the demo agents once.

    Called lazily on the first request so it also works on serverless hosts
    that do not run ASGI startup events. Safe to call repeatedly.
    """
    global _initialised
    if _initialised:
        return
    from . import models  # noqa: F401  (registers tables on Base)

    try:
        if IS_POSTGRES:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(engine)
        if IS_POSTGRES:
            with engine.begin() as conn:
                # HNSW gives ~O(log N) nearest-neighbour search at scale.
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS agents_preference_hnsw ON agents "
                    "USING hnsw (preference_embedding vector_cosine_ops)"
                ))
    except Exception as exc:  # two cold starts racing: the other one created it
        log.warning("init_db: schema step raised %s (continuing)", exc)

    if seed:
        from .seed import seed_if_empty

        with SessionLocal() as db:
            seed_if_empty(db)
    _initialised = True


def reset_db(seed: bool = True) -> None:
    """Drop everything and start again (tests and the demo reset button)."""
    global _initialised
    from . import models  # noqa: F401

    Base.metadata.drop_all(engine)
    _initialised = False
    init_db(seed=seed)


def get_db() -> Iterator[Session]:
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
