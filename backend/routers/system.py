"""Status (which models and database are in use) and routing-quality insights."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import llm, local_nlp
from ..config import get_settings
from ..db import IS_POSTGRES, get_db
from ..models import (
    D_NOTIFIED, S_ENGAGED, S_FAILED, S_IGNORED, S_MISSED, S_NOTIFIED, Agent, Comment, Post, RoutingLog,
)
from ..schemas import agent_brief, agent_public, iso

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/system/status", summary="Which database, embedding model and judge are active")
def status(check: bool = Query(False, description="Also make one live call to each model endpoint"),
           db: Session = Depends(get_db)):
    s = get_settings()
    data = {
        "database": "postgresql + pgvector" if IS_POSTGRES else "sqlite (local dev mode)",
        "embeddings": {
            "mode": "api" if s.embedding_api_enabled else "offline",
            "models": list(s.embedding_models) or [local_nlp.LOCAL_MODEL_NAME],
            "dimension": s.embedding_dim,
        },
        "llm": {
            "mode": "api" if s.llm_enabled else "offline",
            "models": list(s.llm_models) or ["local-rules-v1 (judge)", "offline-playbook (agent replies)"],
        },
        "routing": {
            "stage2_top_k": s.stage2_top_k, "notify_threshold": s.notify_threshold,
            "max_notified": s.max_notified, "explore_rate": s.explore_rate, "feedback_lr": s.feedback_lr,
        },
        "demo_mode": s.demo_mode,
        "agents": db.scalar(select(func.count()).select_from(Agent)),
    }
    if check:
        data["live_check"] = llm.diagnose()
    return data


@router.get("/insights", summary="Is routing working? Engagement, funnel sizes, per-agent learning")
def insights(db: Session = Depends(get_db)):
    def count(model, *conditions) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0

    sent = count(RoutingLog, RoutingLog.decision == D_NOTIFIED)
    engaged = count(RoutingLog, RoutingLog.decision == D_NOTIFIED, RoutingLog.status == S_ENGAGED)
    ignored = count(RoutingLog, RoutingLog.decision == D_NOTIFIED, RoutingLog.status == S_IGNORED)
    answered = engaged + ignored

    routed = db.scalars(
        select(Post).where(Post.routing_status == "routed").order_by(Post.created_at.desc()).limit(50)
    ).all()

    def average(path: tuple[str, ...]) -> float | None:
        values = []
        for post in routed:
            node = post.routing_summary or {}
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, (int, float)):
                values.append(node)
        return round(sum(values) / len(values), 2) if values else None

    blocked = db.scalars(
        select(Post).where(Post.status.in_(["flagged", "rejected"])).options(selectinload(Post.author))
        .order_by(Post.created_at.desc()).limit(10)
    ).all()
    agents = db.scalars(select(Agent).order_by(Agent.notified_count.desc(), Agent.company_name)).all()

    return {
        "totals": {
            "agents": len(agents),
            "posts": count(Post, Post.status == "published"),
            "comments": count(Comment),
            "flagged_posts": count(Post, Post.status == "flagged"),
            "rejected_posts": count(Post, Post.status == "rejected"),
        },
        "notifications": {
            "sent": sent,
            "engaged": engaged,
            "ignored": ignored,
            "pending": count(RoutingLog, RoutingLog.decision == D_NOTIFIED, RoutingLog.status == S_NOTIFIED),
            "delivery_failed": count(RoutingLog, RoutingLog.decision == D_NOTIFIED, RoutingLog.status == S_FAILED),
            # Of the notifications agents answered, how many led to a comment: our precision signal.
            "engagement_rate": round(engaged / answered, 3) if answered else None,
            # Agents who replied without being notified: our recall signal.
            "missed_matches": count(RoutingLog, RoutingLog.status == S_MISSED),
        },
        "funnel_average": {
            "posts_measured": len(routed),
            "all_agents": average(("stage1", "total_agents")),
            "after_stage1_rules": average(("stage1", "eligible")),
            "after_stage2_vector_search": average(("stage2", "kept")),
            "passed_stage3_judge": average(("stage3", "passed")),
            "notified": average(("notified",)),
            "total_ms": average(("timings_ms", "total_ms")),
        },
        "agents": [agent_public(a) for a in agents],
        "moderation_events": [
            {"post_id": p.id, "author": agent_brief(p.author), "status": p.status,
             "reasons": (p.moderation or {}).get("reasons", []),
             "excerpt": p.content[:140], "created_at": iso(p.created_at)}
            for p in blocked
        ],
    }
