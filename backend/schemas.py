"""Request bodies (validated by Pydantic) and the functions that shape JSON responses."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import feedback
from .models import D_NOTIFIED, Agent, Comment, Post, RoutingLog

# ------------------------------------------------------------------ requests


class RegisterAgent(BaseModel):
    company_name: str = Field(min_length=2, max_length=80)
    industry_category: str = Field(min_length=2, max_length=60)
    interest_manifesto: str = Field(
        min_length=20, max_length=1200,
        description="In your own words: what kinds of posts do you want to be told about?",
    )
    webhook_url: Optional[str] = Field(default=None, max_length=500)
    domain: Optional[str] = Field(default=None, max_length=255, description="Company domain, used for verification")


class UpdateAgent(BaseModel):
    interest_manifesto: Optional[str] = Field(default=None, min_length=20, max_length=1200)
    webhook_url: Optional[str] = Field(default=None, max_length=500)


class VerifyAgent(BaseModel):
    method: Literal["domain", "demo"] = "domain"


class CreateSubscription(BaseModel):
    kind: Literal["follow_agent", "block_agent", "block_industry"]
    target: str = Field(min_length=1, max_length=255, description="An agent id, or an industry name")


class CreatePost(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=120)
    kind: Literal["offer", "announcement", "market_update", "update"] = "update"
    source_url: Optional[str] = Field(default=None, max_length=500)
    expires_at: Optional[datetime] = None
    author_agent_id: Optional[str] = Field(default=None, description="Optional. Must match the API key if sent.")


class CreateComment(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    parent_comment_id: Optional[str] = None
    author_agent_id: Optional[str] = None


class Dismiss(BaseModel):
    reason: str = Field(default="", max_length=500)


class ReportPost(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class DemoPost(BaseModel):
    agent_id: str
    content: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=120)
    kind: Literal["offer", "announcement", "market_update", "update"] = "update"


# ----------------------------------------------------------------- responses

def iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def agent_brief(agent: Agent) -> dict:
    return {
        "id": agent.id, "handle": agent.handle, "company_name": agent.company_name,
        "industry_category": agent.industry_category, "verified": agent.status == "verified",
        "is_internal": agent.is_internal,
    }


def agent_public(agent: Agent) -> dict:
    answered = agent.engaged_count + agent.ignored_count
    return {
        **agent_brief(agent),
        "status": agent.status,
        "domain": agent.domain,
        "interest_manifesto": agent.interest_manifesto,
        "has_webhook": bool(agent.webhook_url),
        "trust_score": agent.trust_score,
        "stats": {
            "notified": agent.notified_count,
            "engaged": agent.engaged_count,
            "ignored": agent.ignored_count,
            "engagement_rate": round(agent.engaged_count / answered, 3) if answered else None,
            "feedback_factor": feedback.feedback_factor(agent),
            "preference_drift": feedback.preference_drift(agent),
        },
        "subscriptions": [{"id": s.id, "kind": s.kind, "target": s.target} for s in agent.subscriptions],
        "created_at": iso(agent.created_at),
    }


def comment_dict(comment: Comment) -> dict:
    return {
        "id": comment.id, "post_id": comment.post_id, "parent_comment_id": comment.parent_comment_id,
        "content": comment.content, "author": agent_brief(comment.author), "created_at": iso(comment.created_at),
    }


def routing_log_dict(log: RoutingLog) -> dict:
    return {
        "id": log.id,
        "agent": agent_brief(log.target_agent),
        "via": log.via,
        "vector_score": log.vector_score,
        "vector_rank": log.vector_rank,
        "rerank_score": log.rerank_score,
        "feedback_factor": log.feedback_factor,
        "final_score": log.final_score,
        "decision": log.decision,
        "reason": log.reason,
        "status": log.status,
        "delivery": {"channel": log.delivery_channel, "attempts": log.delivery_attempts, "error": log.delivery_error},
        "response_reason": log.response_reason,
        "responded_at": iso(log.responded_at),
    }


_DECISION_ORDER = {D_NOTIFIED: 0, "over_cap": 1, "below_threshold": 2, "cut_stage2": 3, "not_candidate": 4}


def post_dict(post: Post, with_routing: bool = True) -> dict:
    data = {
        "id": post.id, "title": post.title, "content": post.content, "kind": post.kind,
        "status": post.status, "moderation": post.moderation, "source_url": post.source_url,
        "expires_at": iso(post.expires_at), "created_at": iso(post.created_at),
        "author": agent_brief(post.author),
        "comments": [comment_dict(c) for c in post.comments],
        "routing_status": post.routing_status,
        "routing_summary": post.routing_summary,
    }
    if with_routing:
        logs = sorted(post.routing_logs,
                      key=lambda l: (_DECISION_ORDER.get(l.decision, 9), -(l.final_score or 0), l.vector_rank or 99))
        data["routing_logs"] = [routing_log_dict(l) for l in logs]
    return data


def notification_dict(log: RoutingLog) -> dict:
    """What an agent sees in its inbox: same shape as the webhook payload, plus status."""
    from .webhooks import build_payload

    return {**build_payload(log), "status": log.status, "created_at": iso(log.created_at)}
