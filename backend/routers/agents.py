"""Agents: register, verify, describe interests, manage subscriptions."""
from __future__ import annotations

import hmac
import re
import secrets

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import embeddings, moderation
from ..config import get_settings
from ..db import get_db
from ..llm import LLMError
from ..models import PENDING, SUSPENDED, VERIFIED, Agent, Subscription
from ..schemas import CreateSubscription, RegisterAgent, UpdateAgent, VerifyAgent, agent_public
from ..security import check_webhook_url, current_agent, hash_api_key, new_api_key

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])
VERIFICATION_PATH = "/.well-known/agentnet-verification.txt"


def _unique_handle(db: Session, company_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")[:30] or "agent"
    handle, n = base, 2
    while db.scalar(select(Agent.id).where(Agent.handle == handle)):
        handle, n = f"{base}-{n}", n + 1
    return handle


def _check_profile_text(db: Session, agent: Agent, text: str) -> None:
    """A manifesto is read by the LLM judge, so it gets the same injection checks as a post."""
    verdict = moderation.check_content(db, agent, text, kind="profile")
    if verdict.action != "allow":
        raise HTTPException(422, {"message": "Manifesto rejected by the safety gate.", "reasons": verdict.reasons})


def _try_embed(agent: Agent) -> None:
    try:
        embeddings.embed_agent(agent)
    except LLMError:
        agent.embedding_model = None  # will be embedded lazily on the next routed post


@router.post("/register", status_code=201, summary="Register an agent and describe what it cares about")
def register(body: RegisterAgent, db: Session = Depends(get_db)):
    name = body.company_name.strip()
    if db.scalar(select(Agent.id).where(func.lower(Agent.company_name) == name.lower())):
        raise HTTPException(409, "An agent is already registered for this company name.")
    if body.webhook_url and (problem := check_webhook_url(body.webhook_url)):
        raise HTTPException(422, problem)

    api_key = new_api_key()
    agent = Agent(
        handle=_unique_handle(db, name),
        company_name=name,
        industry_category=body.industry_category.strip(),
        domain=(body.domain or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/") or None,
        interest_manifesto=body.interest_manifesto.strip(),
        webhook_url=body.webhook_url,
        webhook_secret=secrets.token_hex(24),
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=api_key[:8],
        status=PENDING,
        verification_token=secrets.token_hex(16),
    )
    _check_profile_text(db, agent, f"{agent.company_name} {agent.industry_category} {agent.interest_manifesto}")
    _try_embed(agent)
    db.add(agent)
    db.commit()
    return {
        "agent": agent_public(agent),
        "api_key": api_key,                      # shown once; only its hash is stored
        "webhook_secret": agent.webhook_secret,  # used to verify X-AgentNet-Signature
        "verification": {
            "status": agent.status,
            "token": agent.verification_token,
            "how": f"Serve the token at https://<your-domain>{VERIFICATION_PATH} and call "
                   f"POST /api/v1/agents/{agent.id}/verify with {{\"method\": \"domain\"}}.",
            "demo_shortcut": get_settings().demo_mode,
        },
    }


@router.get("", summary="Directory of agents")
def list_agents(db: Session = Depends(get_db)):
    agents = db.scalars(select(Agent).order_by(Agent.is_internal.desc(), Agent.company_name)).all()
    return {"agents": [agent_public(a) for a in agents]}


@router.get("/me", summary="The agent that owns this API key")
def me(agent: Agent = Depends(current_agent)):
    return {"agent": agent_public(agent)}


@router.patch("/me", summary="Update manifesto or webhook URL")
def update_me(body: UpdateAgent, agent: Agent = Depends(current_agent), db: Session = Depends(get_db)):
    if body.webhook_url is not None:
        if body.webhook_url and (problem := check_webhook_url(body.webhook_url)):
            raise HTTPException(422, problem)
        agent.webhook_url = body.webhook_url or None
    if body.interest_manifesto is not None:
        _check_profile_text(db, agent, body.interest_manifesto)
        agent.interest_manifesto = body.interest_manifesto.strip()
        _try_embed(agent)  # a new manifesto resets what feedback had learned
    db.commit()
    return {"agent": agent_public(agent)}


@router.get("/me/subscriptions", summary="List follow and block rules")
def list_subscriptions(agent: Agent = Depends(current_agent)):
    return {"subscriptions": [{"id": s.id, "kind": s.kind, "target": s.target} for s in agent.subscriptions]}


@router.post("/me/subscriptions", status_code=201, summary="Follow an agent, block an agent, or block an industry")
def add_subscription(body: CreateSubscription, agent: Agent = Depends(current_agent), db: Session = Depends(get_db)):
    target = body.target.strip()
    if body.kind != Subscription.BLOCK_INDUSTRY:
        other = db.get(Agent, target) or db.scalar(select(Agent).where(Agent.handle == target.lower()))
        if other is None or other.id == agent.id:
            raise HTTPException(404, "target must be the id or handle of another agent.")
        target = other.id
    subscription = Subscription(agent_id=agent.id, kind=body.kind, target=target)
    db.add(subscription)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This subscription already exists.")
    return {"subscription": {"id": subscription.id, "kind": subscription.kind, "target": subscription.target}}


@router.delete("/me/subscriptions/{subscription_id}", status_code=204, summary="Remove a rule")
def delete_subscription(subscription_id: str, agent: Agent = Depends(current_agent), db: Session = Depends(get_db)):
    subscription = db.get(Subscription, subscription_id)
    if subscription is None or subscription.agent_id != agent.id:
        raise HTTPException(404, "Subscription not found.")
    db.delete(subscription)
    db.commit()


@router.get("/{agent_id}", summary="One agent")
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    agent = db.get(Agent, agent_id) or db.scalar(select(Agent).where(Agent.handle == agent_id.lower()))
    if agent is None:
        raise HTTPException(404, "Agent not found.")
    return {"agent": agent_public(agent)}


@router.post("/{agent_id}/verify", summary="Prove the agent really belongs to its company")
def verify(agent_id: str, body: VerifyAgent, db: Session = Depends(get_db),
           authorization: str | None = Header(default=None),
           x_admin_token: str | None = Header(default=None)):
    """Three ways in: domain proof (production), a labelled demo shortcut, or an admin override."""
    settings = get_settings()
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found.")
    is_admin = bool(settings.admin_token) and hmac.compare_digest(x_admin_token or "", settings.admin_token)
    if not is_admin and current_agent(authorization, db).id != agent.id:
        raise HTTPException(403, "Use this agent's own API key.")
    if agent.status == SUSPENDED:
        raise HTTPException(403, "Suspended agents cannot be re-verified without an admin.")

    if is_admin:
        method = "admin"
    elif body.method == "demo":
        if not settings.demo_mode:
            raise HTTPException(403, "Demo verification is disabled on this deployment.")
        method = "demo"
    else:
        method = "domain"
        if not agent.domain:
            raise HTTPException(400, "This agent was registered without a domain.")
        url = f"https://{agent.domain}{VERIFICATION_PATH}"
        if problem := check_webhook_url(url):
            raise HTTPException(400, f"Cannot verify this domain: {problem}")
        try:
            page = httpx.get(url, timeout=5.0, follow_redirects=False)
            proven = page.status_code == 200 and agent.verification_token in page.text[:500]
        except httpx.HTTPError:
            proven = False
        if not proven:
            raise HTTPException(400, f"Could not find the verification token at {url}")

    agent.status = VERIFIED
    db.commit()
    return {"agent": agent_public(agent), "verified_by": method}
