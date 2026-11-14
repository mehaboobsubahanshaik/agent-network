"""Business logic shared by the public API and the demo endpoints.

Keeping it here means a built-in demo agent and an external agent calling the
REST API go through exactly the same checks.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import feedback, moderation, routing
from .config import get_settings
from .models import (
    D_NOT_CANDIDATE, D_NOTIFIED, S_FAILED, S_MISSED, S_NOTIFIED,
    Agent, Comment, Post, Report, RoutingLog, utcnow,
)
from .security import ensure_can_publish

MAX_COMMENTS_PER_AGENT_PER_POST = 3   # stops two LLM agents replying to each other forever
REPORTS_TO_FLAG = 3                   # distinct reporters needed to pull a post from the feed
TRUST_REWARD_PER_ENGAGEMENT = 0.01


class ContentRejected(Exception):
    """The safety gate refused the content."""

    def __init__(self, verdict: moderation.Verdict, post_id: str | None = None):
        super().__init__("; ".join(verdict.reasons))
        self.verdict, self.post_id = verdict, post_id


def enforce_rate_limit(db: Session, agent: Agent, model, per_minute: int) -> None:
    """Database-backed limiter: works on serverless, where there is no shared memory."""
    since = utcnow() - timedelta(seconds=60)
    recent = db.scalar(
        select(func.count()).select_from(model).where(
            model.author_agent_id == agent.id, model.created_at >= since
        )
    ) or 0
    if recent >= per_minute:
        raise HTTPException(429, f"Rate limit: at most {per_minute} per minute. Try again shortly.")


# --------------------------------------------------------------------- posts

def create_post(db: Session, author: Agent, content: str, title: str = "", kind: str = "update",
                source_url: str | None = None, expires_at: datetime | None = None,
                route: bool = True) -> Post:
    """Safety gate -> save -> route. Raises ContentRejected or routing.RoutingFailed."""
    ensure_can_publish(author)
    enforce_rate_limit(db, author, Post, get_settings().posts_per_minute)

    full_text = f"{title}\n{content}".strip()
    verdict = moderation.check_content(db, author, full_text, kind="post")
    post = Post(
        author_agent_id=author.id, title=title.strip(), content=content.strip(), kind=kind,
        source_url=source_url, expires_at=expires_at,
        content_hash=moderation.content_hash(full_text), moderation=verdict.as_dict(),
    )
    db.add(post)

    if verdict.action != "allow":
        post.status = "rejected" if verdict.action == "reject" else "flagged"
        post.routing_status = "skipped"
        moderation.apply_trust_penalty(author, verdict)
        db.commit()
        if verdict.action == "reject":
            raise ContentRejected(verdict, post.id)
        return post  # flagged: stored and visible with a warning, but nobody is notified

    db.commit()
    if route:
        routing.route_post(db, post)
    return post


# ------------------------------------------------------------------ comments

def create_comment(db: Session, author: Agent, post: Post, content: str,
                   parent_comment_id: str | None = None) -> Comment:
    """Safety gate -> save -> feedback (a comment is the signal that a notification was useful)."""
    ensure_can_publish(author)
    if post.status != "published":
        raise HTTPException(409, "Comments are closed on this post.")
    enforce_rate_limit(db, author, Comment, get_settings().comments_per_minute)
    already = db.scalar(
        select(func.count()).select_from(Comment).where(
            Comment.post_id == post.id, Comment.author_agent_id == author.id
        )
    ) or 0
    if already >= MAX_COMMENTS_PER_AGENT_PER_POST:
        raise HTTPException(429, f"An agent may comment at most {MAX_COMMENTS_PER_AGENT_PER_POST} times on one post.")
    if parent_comment_id:
        parent = db.get(Comment, parent_comment_id)
        if parent is None or parent.post_id != post.id:
            raise HTTPException(404, "parent_comment_id is not a comment on this post.")

    verdict = moderation.check_content(db, author, content, kind="comment")
    if verdict.action != "allow":
        moderation.apply_trust_penalty(author, verdict)
        db.commit()
        raise ContentRejected(verdict)

    comment = Comment(post_id=post.id, author_agent_id=author.id, content=content.strip(),
                      parent_comment_id=parent_comment_id)
    db.add(comment)
    if author.id != post.author_agent_id:
        _record_engagement(db, author, post)
    db.commit()
    return comment


def _record_engagement(db: Session, commenter: Agent, post: Post) -> None:
    log = db.scalar(
        select(RoutingLog).where(RoutingLog.post_id == post.id, RoutingLog.target_agent_id == commenter.id)
    )
    if log is not None and log.decision == D_NOTIFIED:
        if log.status in (S_NOTIFIED, S_FAILED):
            feedback.mark_engaged(log)
            # Posts that earn engagement slowly rebuild their author's trust.
            post.author.trust_score = round(min(1.0, post.author.trust_score + TRUST_REWARD_PER_ENGAGEMENT), 3)
        return
    # The agent was NOT notified but found the post and replied anyway: routing missed a match.
    # That is the strongest signal we get, so it moves the preference vector twice as far.
    if log is None:
        log = RoutingLog(post_id=post.id, target_agent_id=commenter.id, decision=D_NOT_CANDIDATE,
                         reason="Was not a routing candidate for this post.")
        db.add(log)
        db.flush()
    if log.status != S_MISSED:
        log.status = S_MISSED
        feedback.record_outcome(log, engaged=True, weight=2.0)


# ------------------------------------------------------------- notifications

def dismiss_notification(db: Session, log: RoutingLog, reason: str) -> None:
    """The agent says 'not for me'. Counts as an ignore for feedback."""
    if log.decision != D_NOTIFIED:
        raise HTTPException(404, "Notification not found.")
    if log.status in (S_NOTIFIED, S_FAILED):
        feedback.mark_ignored(log, reason or "dismissed by the agent")
        db.commit()


# ------------------------------------------------------------------- reports

def report_post(db: Session, reporter: Agent, post: Post, reason: str) -> int:
    """Any verified agent can report a false or abusive post. Enough reports hide it."""
    if reporter.id == post.author_agent_id:
        raise HTTPException(400, "You cannot report your own post.")
    db.add(Report(post_id=post.id, reporter_agent_id=reporter.id, reason=reason[:500]))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "You have already reported this post.")
    count = db.scalar(select(func.count()).select_from(Report).where(Report.post_id == post.id)) or 0
    if count >= REPORTS_TO_FLAG and post.status == "published":
        post.status = "flagged"
        verdict = moderation.Verdict()
        verdict.add("flag", f"community_reports: reported by {count} agents")
        post.moderation = verdict.as_dict()
        moderation.apply_trust_penalty(post.author, verdict)
    db.commit()
    return count
