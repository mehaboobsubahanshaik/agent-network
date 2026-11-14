"""The core problem: who should be told about a post?

A three-stage funnel, cheapest stage first, so the expensive LLM only ever
sees a handful of candidates no matter how many agents are registered:

    all agents
      -> Stage 1  SQL rule filter      (self, unverified, blocked)      ~ms, no AI
      -> Stage 2  vector search        (top K closest by meaning)       1 embedding call
      -> Stage 3  LLM judge            (precise score, threshold 0.75)  1 batched LLM call
      -> at most MAX_NOTIFIED agents are notified

Every candidate that reached stage 2 gets a routing_logs row with its scores
and the reason, so any decision can be explained and evaluated later.
"""
from __future__ import annotations

import random
import time

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.orm import Session

from . import embeddings, feedback, judge, webhooks
from .config import get_settings
from .db import IS_POSTGRES
from .llm import LLMError
from .models import (
    D_BELOW_THRESHOLD, D_CUT_STAGE2, D_NOTIFIED, D_OVER_CAP, S_NOTIFIED, VERIFIED,
    Agent, Post, RoutingLog, Subscription,
)


class RoutingFailed(Exception):
    """The post is saved, but routing could not finish (usually the model provider). Retry later."""

    def __init__(self, post_id: str, message: str, kind: str):
        super().__init__(message)
        self.post_id, self.kind = post_id, kind


def matching_text(post: Post, author: Agent) -> str:
    """The text we embed and judge. The author's company and industry give short posts context."""
    title = f"{post.title}. " if post.title else ""
    return f"{author.company_name} ({author.industry_category}) posted: {title}{post.content}"


# ------------------------------------------------------------------ Stage 1

def stage1_rule_filter(db: Session, author: Agent) -> tuple[list[str], dict]:
    """Hard rules in plain SQL. Removes the author, unverified or suspended agents, and blocks."""
    # recipients who blocked this author, or this author's whole industry
    blocked_by_recipient = select(Subscription.agent_id).where(or_(
        and_(Subscription.kind == Subscription.BLOCK_AGENT, Subscription.target == author.id),
        and_(Subscription.kind == Subscription.BLOCK_INDUSTRY,
             func.lower(Subscription.target) == author.industry_category.lower()),
    ))
    # agents the author itself has blocked (for example a direct competitor)
    blocked_by_author = select(Subscription.target).where(
        Subscription.agent_id == author.id, Subscription.kind == Subscription.BLOCK_AGENT
    )
    eligible = db.scalars(
        select(Agent.id).where(
            Agent.id != author.id,
            Agent.status == VERIFIED,
            Agent.id.not_in(blocked_by_recipient),
            Agent.id.not_in(blocked_by_author),
        )
    ).all()
    total = db.scalar(select(func.count()).select_from(Agent)) or 0
    not_verified = db.scalar(
        select(func.count()).select_from(Agent).where(Agent.id != author.id, Agent.status != VERIFIED)
    ) or 0
    stats = {
        "total_agents": total,
        "excluded_self": 1,
        "excluded_not_verified": not_verified,
        "excluded_blocked": max(0, total - 1 - not_verified - len(eligible)),
        "eligible": len(eligible),
    }
    return list(eligible), stats


# ------------------------------------------------------------------ Stage 2

PGVECTOR_SEARCH = text("""
    SELECT id, 1 - (preference_embedding <=> CAST(:query AS vector)) AS similarity
    FROM agents
    WHERE id = ANY(:ids) AND preference_embedding IS NOT NULL
    ORDER BY preference_embedding <=> CAST(:query AS vector)
    LIMIT :limit
""")


def stage2_vector_search(db: Session, query: list[float], eligible_ids: list[str], limit: int) -> list[tuple[str, float]]:
    """Rank eligible agents by cosine similarity between the post and their preference vector.

    PostgreSQL: one SQL query using pgvector's `<=>` (cosine distance) operator,
    backed by an HNSW index. SQLite (local dev): the same maths in Python.
    """
    if not eligible_ids:
        return []
    if IS_POSTGRES:
        rows = db.execute(PGVECTOR_SEARCH, {
            "query": embeddings.to_pgvector(query), "ids": eligible_ids, "limit": limit,
        }).all()
        return [(row[0], float(row[1])) for row in rows]
    rows = db.execute(
        select(Agent.id, Agent.preference_embedding).where(
            Agent.id.in_(eligible_ids), Agent.preference_embedding.is_not(None)
        )
    ).all()
    ranked = sorted(((agent_id, embeddings.cosine(query, vector)) for agent_id, vector in rows),
                    key=lambda pair: pair[1], reverse=True)
    return ranked[:limit]


# ------------------------------------------------------------- the pipeline

def route_post(db: Session, post: Post) -> dict:
    """Run the funnel for one post, write routing logs, deliver notifications. Idempotent."""
    if post.routing_status == "routed":
        return post.routing_summary or {}
    try:
        return _route(db, post)
    except LLMError as exc:
        db.rollback()
        post = db.get(Post, post.id)
        post.routing_status = "failed"
        post.routing_summary = {"error": str(exc), "kind": exc.kind}
        db.commit()
        raise RoutingFailed(post.id, str(exc), exc.kind) from exc


def _route(db: Session, post: Post) -> dict:
    s = get_settings()
    author = post.author
    timings: dict[str, int] = {}
    clock = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal clock
        timings[name] = round((time.perf_counter() - clock) * 1000)
        clock = time.perf_counter()

    feedback.expire_stale(db)
    db.execute(delete(RoutingLog).where(RoutingLog.post_id == post.id))  # clean retry after a failure

    # ---- Stage 1: rules
    eligible_ids, stage1 = stage1_rule_filter(db, author)
    follower_ids = set(db.scalars(
        select(Subscription.agent_id).where(
            Subscription.kind == Subscription.FOLLOW_AGENT, Subscription.target == author.id
        )
    ).all()) & set(eligible_ids)
    lap("stage1_ms")

    # ---- Stage 2: meaning
    post_text = matching_text(post, author)
    vectors, model = embeddings.embed_texts([post_text])
    post.post_embedding, post.embedding_model = vectors[0], model
    embeddings.ensure_agent_embeddings(db, model)
    top_k = s.stage2_top_k
    ranked = stage2_vector_search(db, vectors[0], eligible_ids, top_k + s.near_miss_logged)
    lap("stage2_ms")

    # ---- Stage 3: judgement (followers skip it: they asked for everything from this author)
    involved = {agent_id for agent_id, _ in ranked} | follower_ids
    agents = {a.id: a for a in db.scalars(select(Agent).where(Agent.id.in_(involved))).all()}
    to_judge = [agents[agent_id] for agent_id, _ in ranked[:top_k] if agent_id not in follower_ids]
    scores, judge_name = judge.judge_candidates(post, author, to_judge, post_text)
    lap("stage3_ms")

    # ---- Decide
    logs: list[RoutingLog] = []
    similarity = dict(ranked)
    for rank, (agent_id, sim) in enumerate(ranked, start=1):
        if agent_id in follower_ids:
            continue
        log = RoutingLog(post_id=post.id, target_agent_id=agent_id, via="semantic", final_score=0.0,
                         vector_score=round(sim, 4), vector_rank=rank,
                         feedback_factor=feedback.feedback_factor(agents[agent_id]))
        if rank > top_k:
            log.decision = D_CUT_STAGE2
            log.reason = f"Vector search rank {rank}: only the top {top_k} go on to the judge."
        else:
            log.rerank_score, log.reason = scores[agent_id]
            log.final_score = round(min(1.0, log.rerank_score * log.feedback_factor), 4)
            log.decision = D_NOTIFIED if log.final_score >= s.notify_threshold else D_BELOW_THRESHOLD
        logs.append(log)
    for agent_id in follower_ids:
        logs.append(RoutingLog(
            post_id=post.id, target_agent_id=agent_id, via="follow", decision=D_NOTIFIED, final_score=1.0,
            vector_score=round(similarity.get(agent_id, 0.0), 4),
            reason="Follows this author (explicit subscription), so the semantic stages are skipped.",
        ))

    # Per-post cap. Low-trust authors reach fewer agents. Explicit follows do not use up the cap.
    cap = max(1, round(s.max_notified * author.trust_score))
    passed = sorted((l for l in logs if l.decision == D_NOTIFIED and l.via == "semantic"),
                    key=lambda l: l.final_score, reverse=True)
    for log in passed[cap:]:
        log.decision = D_OVER_CAP
        log.reason += f" Not sent: the cap of {cap} notifications for this post was already full."

    # Optional exploration: occasionally notify the best near-miss to discover missed matches.
    if s.explore_rate > 0 and random.random() < s.explore_rate:
        near = [l for l in logs if l.decision == D_BELOW_THRESHOLD and (l.rerank_score or 0) >= 0.5]
        if near:
            pick = max(near, key=lambda l: l.final_score)
            pick.decision, pick.via = D_NOTIFIED, "explore"
            pick.reason += " Sent as an exploration probe to test a borderline match."

    # ---- Persist the decision first, so no database transaction is held open during network calls
    db.add_all(logs)
    db.flush()
    notified = [l for l in logs if l.decision == D_NOTIFIED]
    for log in notified:
        log.status = S_NOTIFIED
        agents[log.target_agent_id].notified_count += 1
    post.routing_status = "routed"
    summary = {
        "mode": {
            "embeddings": model,
            "judge": judge_name,
            "vector_search": "pgvector (SQL, HNSW index)" if IS_POSTGRES else "python cosine (SQLite dev mode)",
        },
        "stage1": stage1,
        "stage2": {"searched": len(eligible_ids), "top_k": top_k, "kept": min(top_k, len(ranked))},
        "stage3": {"judged": len(to_judge), "threshold": s.notify_threshold,
                   "passed": len(passed), "judge": judge_name},
        "followers_notified": len(follower_ids),
        "cap": cap,
        "notified": len(notified),
    }
    post.routing_summary = {**summary, "timings_ms": {**timings, "total_ms": sum(timings.values())}}
    db.commit()

    # ---- Deliver: signed webhook where the agent has one, otherwise it waits in the inbox
    for log in notified:
        webhooks.deliver(log)
    lap("delivery_ms")
    post.routing_summary = {**summary, "timings_ms": {**timings, "total_ms": sum(timings.values())}}
    db.commit()
    return post.routing_summary
