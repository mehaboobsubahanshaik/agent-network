"""One place that turns text into vectors, plus small vector helpers.

API key configured  -> real embeddings from the provider (llm.embed_texts)
no key              -> the offline concept embedder (local_nlp.local_embed)

Every stored vector is tagged with the model that produced it, because
vectors from different models must never be compared with each other.
"""
from __future__ import annotations

import math

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import llm, local_nlp
from .config import get_settings
from .models import Agent


def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    s = get_settings()
    if s.embedding_api_enabled:
        return llm.embed_texts(texts)
    return [local_nlp.local_embed(t, s.embedding_dim) for t in texts], local_nlp.LOCAL_MODEL_NAME


def embed_agent(agent: Agent) -> None:
    """(Re)compute an agent's interest vector from its manifesto and reset what it has learned."""
    vectors, model = embed_texts([agent.interest_manifesto])
    agent.interest_embedding = vectors[0]
    agent.preference_embedding = vectors[0]
    agent.embedding_model = model


def ensure_agent_embeddings(db: Session, model: str) -> int:
    """Embed any agent whose vector is missing or came from a different model.

    Normally a no-op. It does real work on the first post after seeding, and
    after the embedding model is changed in the environment.
    """
    stale = db.scalars(
        select(Agent).where(or_(Agent.embedding_model.is_(None), Agent.embedding_model != model))
    ).all()
    if not stale:
        return 0
    vectors, new_model = embed_texts([a.interest_manifesto for a in stale])
    for agent, vector in zip(stale, vectors):
        agent.interest_embedding = vector
        agent.preference_embedding = vector
        agent.embedding_model = new_model
    db.flush()
    return len(stale)


# ------------------------------------------------------------ vector helpers

def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def normalise(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm else list(v)


def to_pgvector(v: list[float]) -> str:
    """Text form pgvector accepts in SQL: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"
