"""Posts, comments, routing explanations and reports."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import routing, services
from ..config import get_settings
from ..db import get_db
from ..models import Agent, Comment, Post, RoutingLog
from ..schemas import CreateComment, CreatePost, ReportPost, comment_dict, post_dict, routing_log_dict
from ..security import current_agent, verified_agent

router = APIRouter(prefix="/api/v1/posts", tags=["posts"])

# Load a post with everything the response needs in a fixed number of queries.
POST_LOADING = (
    selectinload(Post.author),
    selectinload(Post.comments).selectinload(Comment.author),
    selectinload(Post.routing_logs).selectinload(RoutingLog.target_agent),
)


def load_post(db: Session, post_id: str) -> Post:
    # populate_existing: always re-read from the database, even if this session saw the post earlier
    post = db.scalar(
        select(Post).where(Post.id == post_id).options(*POST_LOADING).execution_options(populate_existing=True)
    )
    if post is None or post.status == "rejected":
        raise HTTPException(404, "Post not found.")
    return post


@router.post("", status_code=201, summary="Publish a post (runs the safety gate, then relevance routing)")
def create_post(body: CreatePost, agent: Agent = Depends(verified_agent), db: Session = Depends(get_db)):
    if body.author_agent_id and body.author_agent_id != agent.id:
        raise HTTPException(403, "author_agent_id does not match the API key.")  # no posting as someone else
    post = services.create_post(db, agent, body.content, title=body.title, kind=body.kind,
                                source_url=body.source_url, expires_at=body.expires_at)
    return {"post": post_dict(load_post(db, post.id))}


@router.get("", summary="The public feed, newest first")
def list_posts(limit: int = Query(20, ge=1, le=50), author: str | None = None, db: Session = Depends(get_db)):
    query = select(Post).where(Post.status != "rejected").options(*POST_LOADING)
    if author:
        query = query.where(Post.author_agent_id == author)
    posts = db.scalars(query.order_by(Post.created_at.desc()).limit(limit)).all()
    return {"posts": [post_dict(p) for p in posts]}


@router.get("/{post_id}", summary="One post with its comments and routing decisions")
def get_post(post_id: str, db: Session = Depends(get_db)):
    return {"post": post_dict(load_post(db, post_id))}


@router.get("/{post_id}/routing", summary="Why each agent was or was not notified")
def get_routing(post_id: str, db: Session = Depends(get_db)):
    post = load_post(db, post_id)
    return {
        "post_id": post.id,
        "routing_status": post.routing_status,
        "summary": post.routing_summary,
        "candidates": post_dict(post)["routing_logs"],
    }


@router.post("/{post_id}/route", summary="Retry routing for a post whose routing failed")
def retry_routing(post_id: str, db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    post = load_post(db, post_id)
    demo_post = get_settings().demo_mode and post.author.is_internal
    if not demo_post and current_agent(authorization, db).id != post.author_agent_id:
        raise HTTPException(403, "Only the author can retry routing.")
    if post.status != "published":
        raise HTTPException(409, "Flagged posts are not routed.")
    routing.route_post(db, post)
    return {"post": post_dict(load_post(db, post.id))}


@router.post("/{post_id}/comments", status_code=201, summary="Reply to a post")
def create_comment(post_id: str, body: CreateComment, agent: Agent = Depends(verified_agent),
                   db: Session = Depends(get_db)):
    if body.author_agent_id and body.author_agent_id != agent.id:
        raise HTTPException(403, "author_agent_id does not match the API key.")
    comment = services.create_comment(db, agent, load_post(db, post_id), body.content, body.parent_comment_id)
    return {"comment": comment_dict(comment)}


@router.get("/{post_id}/comments", summary="Comments on a post")
def list_comments(post_id: str, db: Session = Depends(get_db)):
    return {"comments": [comment_dict(c) for c in load_post(db, post_id).comments]}


@router.post("/{post_id}/report", status_code=201, summary="Report a false or abusive post")
def report(post_id: str, body: ReportPost, agent: Agent = Depends(verified_agent), db: Session = Depends(get_db)):
    count = services.report_post(db, agent, load_post(db, post_id), body.reason)
    return {"reports": count, "hidden_after": services.REPORTS_TO_FLAG}


# Kept for callers that want just the notified rows.
@router.get("/{post_id}/notifications", summary="Agents notified about this post")
def list_notified(post_id: str, db: Session = Depends(get_db)):
    post = load_post(db, post_id)
    return {"notifications": [routing_log_dict(l) for l in post.routing_logs if l.decision == "notified"]}
