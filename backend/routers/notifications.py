"""The pull channel: an agent reads its inbox with its API key instead of receiving webhooks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import services
from ..db import get_db
from ..models import D_NOTIFIED, S_FAILED, S_NOTIFIED, Agent, RoutingLog
from ..schemas import Dismiss, notification_dict
from ..security import current_agent

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("", summary="This agent's notifications (pending ones by default)")
def inbox(status: str | None = Query(None, description="NOTIFIED, ENGAGED, IGNORED or FAILED"),
          limit: int = Query(50, ge=1, le=100),
          agent: Agent = Depends(current_agent), db: Session = Depends(get_db)):
    query = select(RoutingLog).where(RoutingLog.target_agent_id == agent.id, RoutingLog.decision == D_NOTIFIED)
    if status:
        query = query.where(RoutingLog.status == status.upper())
    else:
        query = query.where(RoutingLog.status.in_([S_NOTIFIED, S_FAILED]))
    logs = db.scalars(query.order_by(RoutingLog.created_at.desc()).limit(limit)).all()
    for log in logs:
        if log.status == S_FAILED:  # the webhook failed, but the agent has now read it here
            log.status, log.delivery_channel = S_NOTIFIED, "inbox"
    db.commit()
    return {"notifications": [notification_dict(l) for l in logs]}


@router.post("/{notification_id}/dismiss", summary="Tell the platform this notification was not useful")
def dismiss(notification_id: str, body: Dismiss, agent: Agent = Depends(current_agent),
            db: Session = Depends(get_db)):
    log = db.get(RoutingLog, notification_id)
    if log is None or log.target_agent_id != agent.id:
        raise HTTPException(404, "Notification not found.")
    services.dismiss_notification(db, log, body.reason)
    return {"notification": notification_dict(log)}
