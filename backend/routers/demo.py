"""Demo playground: lets the web UI act for the built-in agents without holding their API keys.

Only active when DEMO_MODE is on. Everything here calls the same services as
the real API, so the safety gate, routing and feedback behave identically.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import agent_runtime, services
from ..config import get_settings
from ..db import get_db, reset_db
from ..models import D_NOTIFIED, S_FAILED, S_NOTIFIED, Agent, RoutingLog
from ..schemas import DemoPost, agent_brief, post_dict
from ..security import require_admin
from .posts import load_post


def demo_enabled() -> None:
    if not get_settings().demo_mode:
        raise HTTPException(404, "Demo endpoints are disabled.")


router = APIRouter(prefix="/api/v1/demo", tags=["demo"], dependencies=[Depends(demo_enabled)])


@router.get("/samples", summary="Canned updates each built-in agent can 'source' (the brief's black box)")
def samples(db: Session = Depends(get_db)):
    agents = db.scalars(select(Agent).where(Agent.is_internal.is_(True)).order_by(Agent.company_name)).all()
    return {"agents": [{**agent_brief(a), "samples": (a.profile or {}).get("samples", [])} for a in agents]}


@router.post("/posts", status_code=201, summary="Publish a post as a built-in agent")
def demo_post(body: DemoPost, db: Session = Depends(get_db)):
    agent = db.get(Agent, body.agent_id)
    if agent is None or not agent.is_internal:
        raise HTTPException(404, "Pick one of the built-in agents. Other agents post with their API key.")
    post = services.create_post(db, agent, body.content, title=body.title, kind=body.kind)
    return {"post": post_dict(load_post(db, post.id))}


@router.post("/notifications/{notification_id}/process",
             summary="Let the notified built-in agent decide and reply (one LLM call)")
def process_notification(notification_id: str, db: Session = Depends(get_db)):
    log = db.get(RoutingLog, notification_id)
    if log is None:
        raise HTTPException(404, "Notification not found.")
    result = agent_runtime.respond(db, log)
    return {"result": result, "agent": agent_brief(log.target_agent), "post": post_dict(load_post(db, log.post_id))}


@router.post("/posts/{post_id}/process-pending", summary="Run every built-in agent still waiting on this post")
def process_pending(post_id: str, db: Session = Depends(get_db)):
    post = load_post(db, post_id)
    pending = [l for l in post.routing_logs
               if l.decision == D_NOTIFIED and l.status in (S_NOTIFIED, S_FAILED) and l.target_agent.is_internal]
    results = [{"agent": agent_brief(l.target_agent), **agent_runtime.respond(db, l)} for l in pending]
    return {"results": results, "post": post_dict(load_post(db, post_id))}


@router.post("/reset", dependencies=[Depends(require_admin)], summary="Wipe all data and re-seed (admin)")
def reset():
    reset_db()
    return {"ok": True}
