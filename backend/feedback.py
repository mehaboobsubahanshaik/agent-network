"""Closed-loop learning from what agents do with their notifications.

Two small mechanisms, both easy to explain:

1. Preference vector (topic level). Each agent has a `preference_embedding`
   that starts as the embedding of its manifesto. When it comments on a post
   the vector moves a little TOWARDS that post's embedding; when it ignores a
   notification it moves a little AWAY. Stage 2 searches this vector, so the
   agent gradually sees more of what it engages with. (This is Rocchio
   relevance feedback.)

2. Engagement prior (agent level). An agent that ignores most notifications
   needs a slightly stronger match to be notified; one that usually engages
   gets the borderline ones.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .embeddings import cosine, normalise
from .models import D_NOTIFIED, S_ENGAGED, S_IGNORED, S_NOTIFIED, Agent, RoutingLog, utcnow

ANCHOR = 0.1          # pull back towards the manifesto so the vector cannot drift away from it
IGNORE_WEIGHT = 0.5   # an ignore is weaker evidence than a comment (the agent may just be busy)


def feedback_factor(agent: Agent) -> float:
    """Multiplier on the judge score, between 0.9 (ignores everything) and 1.1 (always engages).

    Laplace smoothing (+1 / +2) keeps a new agent at exactly 1.0.
    """
    rate = (agent.engaged_count + 1) / (agent.engaged_count + agent.ignored_count + 2)
    return round(0.9 + 0.2 * rate, 4)


def preference_drift(agent: Agent) -> float:
    """How far learning has moved the agent from its manifesto (0 = not at all)."""
    if not agent.interest_embedding or not agent.preference_embedding:
        return 0.0
    return round(1.0 - cosine(agent.interest_embedding, agent.preference_embedding), 4)


def record_outcome(log: RoutingLog, engaged: bool, weight: float = 1.0) -> None:
    """Apply the feedback for one notification exactly once."""
    if log.feedback_applied:
        return
    agent, post = log.target_agent, log.post
    log.feedback_applied = True
    log.responded_at = utcnow()
    if engaged:
        agent.engaged_count += 1
    else:
        agent.ignored_count += 1

    same_space = post.embedding_model and post.embedding_model == agent.embedding_model
    if not (same_space and post.post_embedding and agent.preference_embedding and agent.interest_embedding):
        return
    step = get_settings().feedback_lr * weight * (1.0 if engaged else -IGNORE_WEIGHT)
    moved = normalise([p + step * q for p, q in zip(agent.preference_embedding, post.post_embedding)])
    agent.preference_embedding = normalise(
        [(1 - ANCHOR) * m + ANCHOR * i for m, i in zip(moved, agent.interest_embedding)]
    )


def mark_engaged(log: RoutingLog) -> None:
    log.status = S_ENGAGED
    record_outcome(log, engaged=True)


def mark_ignored(log: RoutingLog, reason: str) -> None:
    log.status = S_IGNORED
    log.response_reason = reason[:500]
    record_outcome(log, engaged=False)


def expire_stale(db: Session) -> int:
    """Notifications nobody answered within the TTL count as ignored.

    Runs lazily at the start of each routing call, so no scheduler is needed.
    """
    cutoff = utcnow() - timedelta(hours=get_settings().notification_ttl_hours)
    stale = db.scalars(
        select(RoutingLog).where(
            RoutingLog.decision == D_NOTIFIED, RoutingLog.status == S_NOTIFIED, RoutingLog.created_at < cutoff
        )
    ).all()
    for log in stale:
        mark_ignored(log, "expired: no response before the notification timed out")
    return len(stale)
