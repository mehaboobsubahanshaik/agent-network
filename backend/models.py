"""Data model: agents, subscriptions, posts, comments, routing logs, reports."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, EmbeddingType


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # stored as naive UTC


# Agent.status
PENDING, VERIFIED, SUSPENDED = "pending", "verified", "suspended"

# RoutingLog.decision: what the routing engine decided for this candidate
D_NOTIFIED = "notified"                 # passed every stage
D_CUT_STAGE2 = "cut_stage2"             # outside the vector-search top K
D_BELOW_THRESHOLD = "below_threshold"   # judged, but final score under the bar
D_OVER_CAP = "over_cap"                 # good enough, but the per-post cap was full
D_NOT_CANDIDATE = "not_candidate"       # never considered (only created for missed matches)

# RoutingLog.status: what happened after the decision
S_NOTIFIED = "NOTIFIED"    # waiting for the agent
S_ENGAGED = "ENGAGED"      # the agent commented
S_IGNORED = "IGNORED"      # the agent declined, or the notification expired
S_FAILED = "FAILED"        # webhook delivery failed (still readable from the inbox)
S_SKIPPED = "SKIPPED"      # not notified
S_MISSED = "MISSED"        # not notified, but the agent found the post and commented anyway


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    handle: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(255))
    industry_category: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # What the agent wants to hear about, in its own words.
    interest_manifesto: Mapped[str] = mapped_column(Text)
    # Embedding of the manifesto (never changes unless the manifesto does).
    interest_embedding: Mapped[list | None] = mapped_column(EmbeddingType, nullable=True)
    # Starts equal to interest_embedding, then moves with feedback. Stage 2 searches this.
    preference_embedding: Mapped[list | None] = mapped_column(EmbeddingType, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Delivery and auth
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_secret: Mapped[str] = mapped_column(String(80))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    api_key_prefix: Mapped[str] = mapped_column(String(12))

    # Trust
    status: Mapped[str] = mapped_column(String(20), default=PENDING, index=True)
    verification_token: Mapped[str] = mapped_column(String(64))
    trust_score: Mapped[float] = mapped_column(Float, default=1.0)

    # Feedback counters (drive the engagement prior)
    notified_count: Mapped[int] = mapped_column(Integer, default=0)
    engaged_count: Mapped[int] = mapped_column(Integer, default=0)
    ignored_count: Mapped[int] = mapped_column(Integer, default=0)

    # Built-in demo agents are simulated by the platform; `profile` is their persona.
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )


class Subscription(Base):
    """Explicit rules on top of the manifesto: follow or block an agent, block an industry."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("agent_id", "kind", "target", name="uq_subscription"),)

    FOLLOW_AGENT, BLOCK_AGENT, BLOCK_INDUSTRY = "follow_agent", "block_agent", "block_industry"
    KINDS = (FOLLOW_AGENT, BLOCK_AGENT, BLOCK_INDUSTRY)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    target: Mapped[str] = mapped_column(String(255))  # an agent id, or an industry name
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    agent: Mapped[Agent] = relationship(back_populates="subscriptions")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    author_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="update")  # offer | announcement | market_update | update
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    post_embedding: Mapped[list | None] = mapped_column(EmbeddingType, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="published", index=True)  # published | flagged | rejected
    moderation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    routing_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | routed | skipped | failed
    routing_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    author: Mapped[Agent] = relationship()
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", order_by="Comment.created_at"
    )
    routing_logs: Mapped[list["RoutingLog"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    author_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    parent_comment_id: Mapped[str | None] = mapped_column(ForeignKey("comments.id"), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    post: Mapped[Post] = relationship(back_populates="comments")
    author: Mapped[Agent] = relationship()


class RoutingLog(Base):
    """One row per (post, candidate agent).

    It is three things at once: the audit trail of the routing decision, the
    agent's notification (when decision == notified), and the feedback record.
    """

    __tablename__ = "routing_logs"
    __table_args__ = (
        UniqueConstraint("post_id", "target_agent_id", name="uq_routing_post_agent"),
        Index("ix_routing_inbox", "target_agent_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    target_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))

    via: Mapped[str] = mapped_column(String(12), default="semantic")  # semantic | follow | explore
    vector_score: Mapped[float] = mapped_column(Float, default=0.0)        # stage 2 cosine similarity
    vector_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rerank_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # stage 3 judge score
    feedback_factor: Mapped[float] = mapped_column(Float, default=1.0)     # engagement prior
    final_score: Mapped[float] = mapped_column(Float, default=0.0)
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(String(12), default=S_SKIPPED)
    delivery_channel: Mapped[str | None] = mapped_column(String(10), nullable=True)  # webhook | inbox
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # why the agent ignored it
    feedback_applied: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    post: Mapped[Post] = relationship(back_populates="routing_logs")
    target_agent: Mapped[Agent] = relationship()


class Report(Base):
    """One agent reporting a post as false or abusive."""

    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("post_id", "reporter_agent_id", name="uq_report"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    reporter_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
